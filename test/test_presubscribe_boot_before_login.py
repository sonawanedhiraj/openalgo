"""End-to-end regression test for the BOOT-BEFORE-LOGIN broker-WS bring-up.

This is the #244 guarantee the existing ``test_scanner_presubscribe_first_of_day``
suite was missing: it spies ``PreSubscriber.ensure`` and feeds the WS getter a
static "is it up?" queue, so it never exercises the *real* lazy-client
AUTH_SUCCESS path — the one mechanism that actually brings the feed up on the
morning the operator boots OpenAlgo before logging in to Zerodha.

The real morning sequence that bit us on 2026-06-30
---------------------------------------------------
1. App boots at 08:31 — no Zerodha session yet, so ``api_key_provider`` returns
   None. ``wire_pre_subscribe`` registers all three triggers but nothing can
   subscribe yet.
2. Operator completes Zerodha OAuth around 09:1x. ``auth_db`` now has an api_key
   and a fresh broker token; ``utils.auth_utils.notify_broker_session_refreshed``
   publishes ``broker_session_refreshed`` on the in-process bus.
3. A trigger fires ``_attempt`` → ``ws_connection_getter(user_id)``. The lazy
   ``WebSocketClient`` is created here and STARTS its connect loop; this first
   call returns ``(False, None, ...)`` — connecting, not yet authenticated —
   and ``_attempt`` returns False without subscribing.
4. Moments later the proxy sends AUTH_SUCCESS. ``websocket_client.py:479`` calls
   ``_fire_connect_callbacks(user_id, broker)``, which invokes the connect
   callback ``wire_pre_subscribe`` registered → ``pre_subscriber.ensure(...,
   reset=True)`` → the full universe is subscribed.

The pre-#244 boot daemon fetched the api_key once, bailed at step 1 because it
was None, and had no event-bus subscriber, so steps 2-4 never happened: zero
ticks all day until a manual restart.

This test models that exact lazy-client handoff with high fidelity:

* a real :class:`PreSubscriber` (NOT a spy) backed by a fake subscribe client,
  so the assertion is on the *actual* set of symbols subscribed;
* a ``_LazyWSConnectionGetter`` that returns ``(False, ...)`` while "connecting"
  and only fires the captured connect callback when the test simulates
  AUTH_SUCCESS — exactly like the real client;
* the connect callback captured via the injected ``register_callback`` (the
  same object ``_fire_connect_callbacks`` would invoke in production).

It proves BOTH independent bring-up paths (event bus and boot-retry poll),
asserts idempotency (no double-subscribe), that a session landing AFTER
``establish()`` starts is still caught, and pins the failure mode: with no login
nothing subscribes and nothing raises.

Pure hermetic — no Flask, no proxy, no broker, no DB. Every dependency is
injected through ``wire_pre_subscribe(start_thread=False, ...)``.
"""

from __future__ import annotations

import pytest

from services.scanner_presubscribe import (
    PreSubscriber,
    PreSubscribeWiring,
    wire_pre_subscribe,
)

# The universe a real wiring would subscribe (a couple of indices mixed in so
# the NSE_INDEX routing is exercised by the real PreSubscriber too).
UNIVERSE = ["RELIANCE", "INFY", "TCS", "NIFTY", "BANKNIFTY"]


# ---------------------------------------------------------------------------
# Fakes that model the REAL lazy broker-WS client
# ---------------------------------------------------------------------------


class _FakeSubscribeClient:
    """Stand-in for the live ``WebSocketClient`` once it is connected+authed.

    ``PreSubscriber.ensure`` calls ``client.subscribe(batch, mode=...)`` and
    reads the per-symbol ``subscriptions`` list. We echo every requested symbol
    back with ``status='success'`` — the broker-accepted ack shape — so the real
    ``ensure`` records exactly the symbols it asked for.
    """

    def __init__(self):
        self.subscribe_calls: list[list[dict]] = []

    def subscribe(self, batch, mode="Quote"):
        self.subscribe_calls.append(list(batch))
        return {
            "status": "success",
            "message": "Subscription processing complete",
            "subscriptions": [
                {"symbol": item["symbol"], "exchange": item["exchange"], "status": "success"}
                for item in batch
            ],
        }


class _LazyWSConnectionGetter:
    """Models the lazy ``get_websocket_connection`` handshake.

    Real semantics (see ``wire_pre_subscribe._attempt`` docstring and
    ``websocket_client.py:479``):

    * The FIRST call (any call before AUTH_SUCCESS) creates the client, kicks its
      connect loop, and returns ``(False, None, "connecting")`` — nothing is
      subscribed yet, and the connection getter registers nothing.
    * When the proxy later sends AUTH_SUCCESS, the client fires the registered
      connect callbacks. We model AUTH_SUCCESS as :meth:`fire_auth_success`,
      which invokes the captured connect callback ``(user_id, broker)`` — the
      same call ``_fire_connect_callbacks`` makes in production.
    * After AUTH_SUCCESS, subsequent getter calls return ``(True, client, None)``
      (the WS is up), so a mid-day ``_attempt`` would ``ensure`` directly.
    """

    def __init__(self):
        self.client = _FakeSubscribeClient()
        self.authenticated = False
        self.call_count = 0
        # Captured connect callback (uid, broker) -> None, set via the injected
        # register_callback. This is the exact object _fire_connect_callbacks
        # would invoke on AUTH_SUCCESS.
        self.connect_callback = None
        self.auth_success_user_id: str | None = None
        self.auth_success_broker: str | None = None

    def __call__(self, user_id):
        """The injected ``ws_connection_getter``."""
        self.call_count += 1
        if self.authenticated:
            return (True, self.client, None)
        # First/early call: lazy client created, connecting, not yet authed.
        return (False, None, "connecting")

    def fire_auth_success(self, user_id="u_VALID_KEY", broker="zerodha"):
        """Simulate the proxy's AUTH_SUCCESS message reaching the client.

        Mirrors ``websocket_client.py`` calling ``_fire_connect_callbacks`` —
        the connect callback registered by ``wire_pre_subscribe`` runs and
        drives ``pre_subscriber.ensure(..., reset=True)``.
        """
        self.authenticated = True
        self.auth_success_user_id = user_id
        self.auth_success_broker = broker
        assert self.connect_callback is not None, (
            "AUTH_SUCCESS fired but no connect callback was registered — "
            "wire_pre_subscribe must register one via register_callback"
        )
        self.connect_callback(user_id, broker)


def _verifier(api_key):
    """``VALID*`` api_key -> ``u_<api_key>`` user id; anything else -> None."""
    return f"u_{api_key}" if api_key and api_key.startswith("VALID") else None


class _FakeBus:
    """Synchronous in-line bus so an assert right after ``publish`` sees effects."""

    def __init__(self):
        self.subscriptions: dict[str, list] = {}

    def subscribe(self, topic, callback, name=""):
        self.subscriptions.setdefault(topic, []).append(callback)

    def publish(self, event):
        for cb in self.subscriptions.get(event.topic, []):
            cb(event)


class _FakeEvent:
    def __init__(self, topic="broker_session_refreshed", username="u_VALID_KEY", broker="zerodha"):
        self.topic = topic
        self.username = username
        self.broker = broker


class _MutableApiKey:
    """A flip-able api_key source. Starts None (boot before login); the test
    flips :attr:`value` to a valid key to model the operator's OAuth landing."""

    def __init__(self, value=None):
        self.value = value

    def __call__(self):
        return self.value


def _wire(*, api_key_source, getter, bus, register_callback, time_seq=None):
    """Build a ``start_thread=False`` wiring with the real PreSubscriber.

    The PreSubscriber's own ``_connection_getter`` is pointed at ``getter`` so
    that ``ensure`` obtains the fake authenticated client and actually
    subscribes — this is the load-bearing difference from the existing suite,
    which spies ``ensure``.
    """
    pre = PreSubscriber("test", lambda s: "NSE_INDEX" if s in {"NIFTY", "BANKNIFTY"} else "NSE")
    pre._connection_getter = getter  # ensure() reaches the fake client through this

    sleep_calls: list[float] = []

    def sleep_fn(sec):
        sleep_calls.append(sec)

    if time_seq is None:
        ticks = iter(range(10_000))
    else:
        ticks = iter(time_seq)

    def time_fn():
        return next(ticks)

    wiring = wire_pre_subscribe(
        "boot_pre_subscribe",
        pre,
        UNIVERSE,
        thread_name="BootTestThread",
        api_key_provider=api_key_source,
        user_id_verifier=_verifier,
        broker_resolver=lambda _k: "zerodha",
        ws_connection_getter=getter,
        register_callback=register_callback,
        bus=bus,
        sleep_fn=sleep_fn,
        time_fn=time_fn,
        retry_sec=1,
        max_wait_sec=60,
        start_thread=False,
    )
    assert isinstance(wiring, PreSubscribeWiring)
    return pre, wiring, sleep_calls


# ---------------------------------------------------------------------------
# Path (a): event bus — the operator's OAuth lands; bus publish brings the feed up
# ---------------------------------------------------------------------------


def test_event_path_boot_before_login_brings_feed_up_via_connect_callback():
    """Boot with no api_key; login lands; the broker_session_refreshed event +
    the lazy-client AUTH_SUCCESS handoff subscribe the FULL universe.

    This is the exact 2026-06-30 morning sequence, now provably covered.
    """
    api_key = _MutableApiKey(value=None)  # boot: no Zerodha session yet
    getter = _LazyWSConnectionGetter()
    bus = _FakeBus()

    captured = {}

    def register_callback(name, cb):
        captured[name] = cb
        getter.connect_callback = cb  # model the WS client holding the callback

    pre, wiring, _ = _wire(
        api_key_source=api_key,
        getter=getter,
        bus=bus,
        register_callback=register_callback,
    )

    # Boot wired all three triggers but nothing is subscribed (no login yet).
    assert "broker_session_refreshed" in bus.subscriptions
    assert captured.get("boot_pre_subscribe") is not None
    assert pre.subscribed == set(), "nothing should be subscribed before login"

    # --- login lands: api_key + user now resolve ---
    api_key.value = "VALID_KEY"

    # Event fires. _attempt runs ws_connection_getter, which is still
    # "connecting" (returns False) — so NO subscribe happens on the event alone.
    bus.publish(_FakeEvent())
    assert pre.subscribed == set(), (
        "the event kicks the connect loop but the lazy client is still "
        "connecting — subscription must wait for AUTH_SUCCESS"
    )
    assert getter.call_count >= 1, "the event must have kicked ws_connection_getter"

    # --- AUTH_SUCCESS arrives: the connect callback fires and subscribes ---
    getter.fire_auth_success(user_id="u_VALID_KEY", broker="zerodha")

    assert pre.subscribed == set(UNIVERSE), (
        "AUTH_SUCCESS must drive ensure() to subscribe the entire universe"
    )
    # The real ensure() routed indices to NSE_INDEX and equities to NSE.
    sub = {item["symbol"]: item["exchange"] for item in getter.client.subscribe_calls[0]}
    assert sub["NIFTY"] == "NSE_INDEX"
    assert sub["BANKNIFTY"] == "NSE_INDEX"
    assert sub["RELIANCE"] == "NSE"


# ---------------------------------------------------------------------------
# Path (b): boot-retry poll — establish() drives the same bring-up
# ---------------------------------------------------------------------------


def test_boot_retry_path_brings_feed_up_when_session_lands_after_establish_starts():
    """establish() is driven synchronously. The api_key is absent for the first
    polls (login not done), then lands mid-loop. The poll re-fetches the key
    every iteration (the #244 fix), reaches an authenticated WS, and subscribes
    the full universe.

    Crucially this proves a session landing AFTER establish() has started is
    still caught — the precise race the pre-#244 single-fetch loop lost.
    """
    getter = _LazyWSConnectionGetter()

    def register_callback(name, cb):
        getter.connect_callback = cb

    # The login completes DURING establish(): the api_key stays None for the
    # first two polls, then flips on the third — modelling the operator's OAuth
    # landing milliseconds after the boot thread started.
    polls = {"n": 0}

    def stepping_api():
        polls["n"] += 1
        return "VALID_KEY" if polls["n"] >= 3 else None

    # Model the lazy client coming up: the first getter call made while a valid
    # user exists kicks the connect loop, and the proxy authenticates — which in
    # production fires the connect callback. We trip AUTH_SUCCESS exactly then,
    # so the NEXT getter call reports the WS up and _attempt subscribes directly.
    raw_call = _LazyWSConnectionGetter.__call__

    def getter_with_auth(user_id):
        if not getter.authenticated:
            getter.fire_auth_success(user_id=user_id, broker="zerodha")
        return raw_call(getter, user_id)

    pre = PreSubscriber("test", lambda s: "NSE_INDEX" if s in {"NIFTY", "BANKNIFTY"} else "NSE")
    pre._connection_getter = getter_with_auth

    sleep_calls: list[float] = []
    ticks = iter(range(10_000))

    wiring = wire_pre_subscribe(
        "boot_pre_subscribe",
        pre,
        UNIVERSE,
        thread_name="BootTestThread",
        api_key_provider=stepping_api,
        user_id_verifier=_verifier,
        broker_resolver=lambda _k: "zerodha",
        ws_connection_getter=getter_with_auth,
        register_callback=register_callback,
        bus=_FakeBus(),
        sleep_fn=lambda s: sleep_calls.append(s),
        time_fn=lambda: next(ticks),
        retry_sec=1,
        max_wait_sec=60,
        start_thread=False,
    )
    assert isinstance(wiring, PreSubscribeWiring)
    assert pre.subscribed == set(), "nothing subscribed before establish() runs"

    wiring.establish()

    assert pre.subscribed == set(UNIVERSE), (
        "boot-retry must catch a session that lands AFTER establish() starts "
        "and subscribe the full universe — the #244 race"
    )
    assert sleep_calls, "the loop must have slept while waiting for the late login"


# ---------------------------------------------------------------------------
# Idempotency: both paths firing must not double-subscribe
# ---------------------------------------------------------------------------


def test_event_then_connect_callback_then_establish_does_not_double_subscribe():
    """All bring-up triggers can fire in sequence; the real PreSubscriber dedups
    by symbol, so the broker subscribe is placed exactly ONCE per symbol."""
    api_key = _MutableApiKey(value="VALID_KEY")  # already logged in
    getter = _LazyWSConnectionGetter()
    bus = _FakeBus()

    def register_callback(name, cb):
        getter.connect_callback = cb

    pre, wiring, _ = _wire(
        api_key_source=api_key,
        getter=getter,
        bus=bus,
        register_callback=register_callback,
    )

    # 1) AUTH_SUCCESS via connect callback (the first connect of the day).
    getter.fire_auth_success()
    assert pre.subscribed == set(UNIVERSE)
    first_batches = len(getter.client.subscribe_calls)
    assert first_batches == 1

    # 2) Event fires afterwards — WS already up, so _attempt calls ensure(), but
    #    every symbol is already tracked → no new subscribe batch.
    bus.publish(_FakeEvent())

    # 3) Boot-retry runs too — WS up on first poll → ensure() again, still a no-op.
    wiring.establish()

    assert pre.subscribed == set(UNIVERSE)
    # ensure() ran 3 times total but only the first placed a broker subscribe;
    # the dedup means no symbol was subscribed twice.
    all_subscribed_symbols = [
        item["symbol"] for batch in getter.client.subscribe_calls for item in batch
    ]
    assert sorted(all_subscribed_symbols) == sorted(UNIVERSE), (
        "idempotent: each symbol subscribed exactly once across all triggers"
    )


# ---------------------------------------------------------------------------
# Failure mode that bit us: no login -> nothing subscribes, nothing raises
# ---------------------------------------------------------------------------


def test_no_login_subscribes_nothing_and_never_raises_then_recovers_on_event():
    """The 2026-06-30 failure mode, pinned.

    If the operator never logs in, none of the three triggers can subscribe and
    NONE may raise (a raising trigger would be worse than a silent miss — it
    could poison the bus dispatcher or crash the boot thread). The moment the
    event eventually fires *with* a session, subscription happens.
    """
    api_key = _MutableApiKey(value=None)  # login never happens (this phase)
    getter = _LazyWSConnectionGetter()
    bus = _FakeBus()

    def register_callback(name, cb):
        getter.connect_callback = cb

    pre, wiring, _ = _wire(
        api_key_source=api_key,
        getter=getter,
        bus=bus,
        register_callback=register_callback,
        # Short ascending clock so the boot loop exits quickly with no api_key.
        time_seq=[0, 0, 1, 1, 2, 2, 3, 3, 100, 100],
    )

    # Event fires with no session — must be a silent no-op, never raising.
    bus.publish(_FakeEvent())
    assert pre.subscribed == set()

    # Boot-retry runs to its deadline with no api_key — must exit without raising.
    wiring.establish()
    assert pre.subscribed == set()
    assert getter.authenticated is False, "no AUTH_SUCCESS without a login"

    # --- now the login finally lands and a fresh event arrives ---
    api_key.value = "VALID_KEY"
    bus.publish(_FakeEvent())  # kicks the lazy client (still connecting → no sub)
    assert pre.subscribed == set()
    getter.fire_auth_success()  # AUTH_SUCCESS → full subscribe
    assert pre.subscribed == set(UNIVERSE), (
        "recovery: once a session lands and AUTH_SUCCESS fires, the universe "
        "subscribes — the feed comes up without a manual restart"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
