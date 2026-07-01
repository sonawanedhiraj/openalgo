"""Tests for the event-driven broker-WebSocket pre-subscribe.

Covers the two halves of the fix for the morning subscribe race
(see services/scanner_presubscribe.py and the connect-callback registry in
services/ws_connect_callbacks.py):

1. The connect-callback registry — register/replace, fan-out firing (each
   callback in its own thread), and fail-safe isolation (a raising callback
   does not block the others).
2. The :class:`PreSubscriber` — idempotent subscribe (a repeat call is a
   no-op), per-symbol response parsing (the proxy's "Subscription processing
   complete" ack is counted as success, not failure), reset-on-reconnect, and
   the NSE_INDEX exchange routing for index symbols.

No live server, broker session, or database is required. The registry lives in
an import-light module (``services.ws_connect_callbacks``) and ``PreSubscriber``
takes an injectable connection getter, so these tests pull in none of the
DB-heavy service modules (importing ``services.websocket_service`` /
``database.auth_db`` blocks while the live app holds the SQLite lock).
"""

from __future__ import annotations

import threading

import pytest

from services import scanner_presubscribe as sps
from services import ws_connect_callbacks as cbreg

# ---------------------------------------------------------------------------
# connect-callback registry
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_callback_registry():
    """Each test starts and ends with an empty connect-callback registry."""
    with cbreg._callback_lock:
        cbreg._connect_callbacks.clear()
    yield
    with cbreg._callback_lock:
        cbreg._connect_callbacks.clear()


def test_register_connect_callback_adds_and_replaces():
    cb1 = lambda uid, brk: None  # noqa: E731
    cb2 = lambda uid, brk: None  # noqa: E731

    cbreg.register_connect_callback("scanner", cb1)
    assert cbreg._connect_callbacks["scanner"] is cb1
    assert len(cbreg._connect_callbacks) == 1

    # Same name replaces, does not stack.
    cbreg.register_connect_callback("scanner", cb2)
    assert cbreg._connect_callbacks["scanner"] is cb2
    assert len(cbreg._connect_callbacks) == 1

    # A different name coexists.
    cbreg.register_connect_callback("regime", cb1)
    assert len(cbreg._connect_callbacks) == 2


def test_unregister_connect_callback_is_noop_when_absent():
    cbreg.register_connect_callback("scanner", lambda u, b: None)
    cbreg.unregister_connect_callback("scanner")
    cbreg.unregister_connect_callback("does-not-exist")  # must not raise
    assert "scanner" not in cbreg._connect_callbacks


def test_fire_invokes_all_callbacks_each_in_its_own_thread():
    seen: list[tuple[str, str, str]] = []  # (name, thread_name, payload)
    done = threading.Barrier(3)  # 2 callbacks + main

    def make_cb(name):
        def _cb(user_id, broker):
            seen.append((name, threading.current_thread().name, f"{user_id}/{broker}"))
            done.wait(timeout=5)

        return _cb

    cbreg.register_connect_callback("scanner", make_cb("scanner"))
    cbreg.register_connect_callback("regime", make_cb("regime"))

    cbreg._fire_connect_callbacks("alice", "zerodha")
    done.wait(timeout=5)  # release once both callbacks have run

    names = {row[0] for row in seen}
    assert names == {"scanner", "regime"}
    # Each ran on its own dedicated, non-main thread.
    thread_names = {row[1] for row in seen}
    assert thread_names == {"connect-cb-scanner", "connect-cb-regime"}
    assert "MainThread" not in thread_names
    # Payload propagated.
    assert all(row[2] == "alice/zerodha" for row in seen)


def test_raising_callback_does_not_block_others():
    good_ran = threading.Event()

    def boom(user_id, broker):
        raise RuntimeError("callback blew up")

    def good(user_id, broker):
        good_ran.set()

    cbreg.register_connect_callback("boom", boom)
    cbreg.register_connect_callback("good", good)

    # Must not raise, and the good callback still fires.
    cbreg._fire_connect_callbacks("u", "b")
    assert good_ran.wait(timeout=5), "good callback did not run after a raising one"


def test_fire_with_no_callbacks_is_noop():
    # Empty registry — must simply return without error.
    cbreg._fire_connect_callbacks("u", "b")


# ---------------------------------------------------------------------------
# resolve_exchange_for_symbol
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_resolver_state():
    """Reset the per-symbol resolution cache and the injected token lookup so
    every test resolves from scratch and never leaks a fake lookup."""
    with sps._resolve_lock:
        sps._resolved_exchange_cache.clear()
    saved = sps._token_lookup
    sps._token_lookup = None
    yield
    sps._token_lookup = saved
    with sps._resolve_lock:
        sps._resolved_exchange_cache.clear()


@pytest.mark.parametrize(
    "symbol,expected",
    [
        # Fast-path broad indices (INDEX_SYMBOLS) — resolve with no DB.
        ("NIFTY", "NSE_INDEX"),
        ("BANKNIFTY", "NSE_INDEX"),
        ("FINNIFTY", "NSE_INDEX"),
        ("MIDCPNIFTY", "NSE_INDEX"),
        ("NIFTYNXT50", "NSE_INDEX"),
        ("INDIAVIX", "NSE_INDEX"),
        ("nifty", "NSE_INDEX"),  # case-insensitive
        # Equities — no master contract injected, default to NSE.
        ("RELIANCE", "NSE"),
        ("TCS", "NSE"),
        ("HDFCBANK", "NSE"),
    ],
)
def test_resolve_exchange_for_symbol(symbol, expected):
    # No _token_lookup injected: fast-path handles the broad indices, everything
    # else falls back to NSE (the master-contract lookup returns None here).
    assert sps.resolve_exchange_for_symbol(symbol) == expected


# The 11 NSE sectoral indices from issue #241 — absent from INDEX_SYMBOLS, so
# they MUST be resolved via the master-contract-backed lookup, not hardcoding.
_SECTORAL_INDICES_241 = [
    "NIFTYAUTO",
    "NIFTYCONSRDURBL",
    "NIFTYCONSUMPTION",
    "NIFTYFMCG",
    "NIFTYIT",
    "NIFTYMETAL",
    "NIFTYOILANDGAS",
    "NIFTYPHARMA",
    "NIFTYPSUBANK",
    "NIFTYPVTBANK",
    "NIFTYREALTY",
]


def _fake_master_contract(index_symbols, equity_symbols):
    """Return a ``(symbol, exchange) -> token | None`` mimicking SymToken.

    ``index_symbols`` are stored under NSE_INDEX; ``equity_symbols`` under NSE.
    """
    index_upper = {s.upper() for s in index_symbols}
    equity_upper = {s.upper() for s in equity_symbols}

    def _lookup(symbol, exchange):
        sym = symbol.upper()
        if exchange == "NSE_INDEX" and sym in index_upper:
            return f"tok-idx-{sym}"
        if exchange == "NSE" and sym in equity_upper:
            return f"tok-eq-{sym}"
        return None

    return _lookup


@pytest.mark.parametrize("symbol", _SECTORAL_INDICES_241)
def test_sectoral_indices_resolve_to_nse_index_via_master_contract(symbol):
    # These 11 are NOT in INDEX_SYMBOLS — proof the fix is not just an extended
    # hardcoded list.
    assert symbol not in sps.INDEX_SYMBOLS
    sps._token_lookup = _fake_master_contract(
        index_symbols=_SECTORAL_INDICES_241, equity_symbols=["RELIANCE", "TCS"]
    )
    assert sps.resolve_exchange_for_symbol(symbol) == "NSE_INDEX"


def test_equities_resolve_to_nse_when_stored_as_equity():
    sps._token_lookup = _fake_master_contract(
        index_symbols=_SECTORAL_INDICES_241, equity_symbols=["RELIANCE", "TCS", "HDFCBANK"]
    )
    for eq in ("RELIANCE", "TCS", "HDFCBANK"):
        assert sps.resolve_exchange_for_symbol(eq) == "NSE"


def test_master_contract_wins_over_default_for_new_index():
    # A brand-new index the hardcoded set has never heard of still routes right
    # as long as the master contract knows it — the whole point of issue #241.
    sps._token_lookup = _fake_master_contract(
        index_symbols=["NIFTYSOMENEWINDEX"], equity_symbols=[]
    )
    assert "NIFTYSOMENEWINDEX" not in sps.INDEX_SYMBOLS
    assert sps.resolve_exchange_for_symbol("NIFTYSOMENEWINDEX") == "NSE_INDEX"


def test_falls_back_to_nse_when_contract_absent():
    # Contract can't answer (get_token returns None for everything) -> NSE, and
    # the unresolved answer is NOT cached, so a later successful lookup wins.
    sps._token_lookup = lambda sym, exch: None
    assert sps.resolve_exchange_for_symbol("NIFTYAUTO") == "NSE"
    # Now the contract loads and knows NIFTYAUTO as an index.
    sps._token_lookup = _fake_master_contract(index_symbols=["NIFTYAUTO"], equity_symbols=[])
    assert sps.resolve_exchange_for_symbol("NIFTYAUTO") == "NSE_INDEX"


def test_fast_path_index_never_touches_contract():
    # INDEX_SYMBOLS members resolve without ever calling the lookup (proven by a
    # lookup that would raise if invoked).
    def _boom(sym, exch):
        raise AssertionError("master-contract lookup must not run for fast-path index")

    sps._token_lookup = _boom
    assert sps.resolve_exchange_for_symbol("NIFTY") == "NSE_INDEX"
    assert sps.resolve_exchange_for_symbol("BANKNIFTY") == "NSE_INDEX"


def test_lookup_exception_falls_back_to_nse():
    # A raising lookup for a non-fast-path symbol must be swallowed to NSE, never
    # propagate into the subscribe path.
    def _boom(sym, exch):
        raise RuntimeError("db exploded")

    sps._token_lookup = _boom
    assert sps.resolve_exchange_for_symbol("SOMEEQUITY") == "NSE"


def test_resolution_is_cached_after_first_success():
    calls = []

    def _counting_lookup(sym, exch):
        calls.append((sym, exch))
        if exch == "NSE_INDEX" and sym == "NIFTYIT":
            return "tok"
        return None

    sps._token_lookup = _counting_lookup
    assert sps.resolve_exchange_for_symbol("NIFTYIT") == "NSE_INDEX"
    n_after_first = len(calls)
    # Second call is served from cache — no further lookups.
    assert sps.resolve_exchange_for_symbol("NIFTYIT") == "NSE_INDEX"
    assert len(calls) == n_after_first


# ---------------------------------------------------------------------------
# PreSubscriber.ensure
# ---------------------------------------------------------------------------


class _FakeClient:
    """Records subscribe calls and returns a canned proxy-style response.

    Mirrors the real proxy/WS-client contract: a ``subscriptions`` list with a
    per-symbol ``status`` plus the generic "Subscription processing complete"
    message and an overall ``status`` that may be ``partial``.
    """

    def __init__(self, fail_symbols=None, overall_status="success"):
        self.fail_symbols = set(fail_symbols or [])
        self.overall_status = overall_status
        self.calls: list[list[dict]] = []

    def subscribe(self, symbols, mode="Quote"):
        self.calls.append(list(symbols))
        per_symbol = []
        for s in symbols:
            sym = s["symbol"]
            if sym in self.fail_symbols:
                per_symbol.append(
                    {
                        "symbol": sym,
                        "exchange": s["exchange"],
                        "status": "error",
                        "message": "Token not found",
                    }
                )
            else:
                per_symbol.append(
                    {"symbol": sym, "exchange": s["exchange"], "status": "success", "mode": mode}
                )
        return {
            "status": self.overall_status,
            "message": "Subscription processing complete",
            "subscriptions": per_symbol,
        }


def _make_subscriber(name, client, exchange_resolver=None):
    """PreSubscriber wired to a fake connection getter (no real WS / DB)."""
    return sps.PreSubscriber(
        name,
        exchange_resolver or sps.resolve_exchange_for_symbol,
        connection_getter=lambda uid: (True, client, None),
    )


def test_ensure_subscribes_and_is_idempotent():
    client = _FakeClient()
    sub = _make_subscriber("scanner", client)

    n1 = sub.ensure("alice", "zerodha", ["RELIANCE", "TCS", "NIFTY"])
    assert n1 == 3
    assert sub.subscribed == {"RELIANCE", "TCS", "NIFTY"}
    assert len(client.calls) == 1

    # Second call: everything already subscribed -> no-op, no new WS call.
    n2 = sub.ensure("alice", "zerodha", ["RELIANCE", "TCS", "NIFTY"])
    assert n2 == 0
    assert len(client.calls) == 1  # client.subscribe NOT called again


def test_ensure_routes_indices_to_nse_index():
    client = _FakeClient()
    sub = _make_subscriber("scanner", client)

    sub.ensure("alice", "zerodha", ["NIFTY", "RELIANCE", "BANKNIFTY"])
    by_symbol = {entry["symbol"]: entry["exchange"] for entry in client.calls[0]}
    assert by_symbol["NIFTY"] == "NSE_INDEX"
    assert by_symbol["BANKNIFTY"] == "NSE_INDEX"
    assert by_symbol["RELIANCE"] == "NSE"


def test_ensure_counts_processing_complete_as_success():
    # Overall status "partial" (one symbol failed) must NOT be misread as a
    # blanket failure: the accepted symbols are still counted via their own
    # per-symbol status.
    client = _FakeClient(fail_symbols={"BADSYM"}, overall_status="partial")
    sub = _make_subscriber("scanner", client)

    n = sub.ensure("alice", "zerodha", ["RELIANCE", "BADSYM", "TCS"])
    assert n == 2  # RELIANCE + TCS counted despite overall "partial"
    assert sub.subscribed == {"RELIANCE", "TCS"}
    # The failed symbol is retried on the next ensure (not tracked as done).
    n2 = sub.ensure("alice", "zerodha", ["RELIANCE", "BADSYM", "TCS"])
    assert n2 == 0  # BADSYM still fails, others already subscribed
    assert "BADSYM" not in sub.subscribed


def test_ensure_reset_resubscribes_all():
    client = _FakeClient()
    sub = _make_subscriber("scanner", client)

    sub.ensure("alice", "zerodha", ["RELIANCE", "TCS"])
    assert len(client.calls) == 1

    # reset=True (the reconnect path) clears tracking and re-subscribes all,
    # because a fresh broker connection has dropped the prior subscriptions.
    sub.ensure("alice", "zerodha", ["RELIANCE", "TCS"], reset=True)
    assert len(client.calls) == 2
    assert {s["symbol"] for s in client.calls[1]} == {"RELIANCE", "TCS"}


def test_ensure_handles_ws_not_available():
    sub = sps.PreSubscriber(
        "scanner",
        sps.resolve_exchange_for_symbol,
        connection_getter=lambda uid: (False, None, "WS not connected"),
    )
    n = sub.ensure("alice", "zerodha", ["RELIANCE"])
    assert n == 0
    assert sub.subscribed == set()  # nothing tracked, will retry later


def test_regime_subscriber_forces_nse_index():
    client = _FakeClient()
    # All regime symbols are indices, even ones not in INDEX_SYMBOLS.
    sub = _make_subscriber("regime", client, exchange_resolver=lambda _s: "NSE_INDEX")
    sub.ensure("alice", "zerodha", ["NIFTYAUTO", "NIFTYIT", "BANKNIFTY"])
    exchanges = {entry["exchange"] for entry in client.calls[0]}
    assert exchanges == {"NSE_INDEX"}
