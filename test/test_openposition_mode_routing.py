"""Issue #507 — the EXIT-path position read must resolve the same book the
order WRITE path chose.

#497/#499 fixed this invariant in ``positionbook_service`` but classified
``openposition_service`` as a UI read. It is not: it feeds
``live_position_reconciliation_service``, whose job is to SUPPRESS an exit when
the store reads flat. So with Analyze OFF a ``sandbox`` strategy wrote its
orders into ``sandbox.db``, read the empty LIVE broker book, and the guard
declared the real position phantom — ``futures_follow_cap50`` had its T+1 exit
suppressed on every attempt from 2026-07-17, stranding 7 NIFTY lots (455 qty)
for three weeks and consuming the whole 50% margin cap so no new entry could
place either.

The invariant these tests pin: **a position opened under
``resolve_order_mode(K)`` is reconciled at exit under ``resolve_order_mode(K)``.**

They drive the REAL chain — ``reconcile_exit`` → ``_fetch_broker_qty`` →
``get_open_position`` → ``get_open_position_with_auth`` → the routing decision.
They deliberately do NOT mock ``get_open_position``: mocking it is exactly what
let this defect survive the existing reconciliation, futures_follow, simplified
engine and EOD-watchdog suites, all of which patch that call.
"""

from unittest.mock import patch

import pytest

from services import live_position_reconciliation_service as recon

# The real stranded position, as it sits in sandbox.db.
_SYMBOL = "NIFTY25AUG26FUT"
_QTY = 455

_SANDBOX_BOOK = (
    True,
    {
        "status": "success",
        "data": [
            {
                "symbol": _SYMBOL,
                "exchange": "NFO",
                "product": "NRML",
                "quantity": _QTY,
                "average_price": "24234.07",
            }
        ],
    },
    200,
)

# The live broker book holds no NIFTY futures for a sandbox-mode strategy. This
# is why the misrouted read returned a clean zero instead of erroring — and why
# the guard trusted it.
_EMPTY_LIVE_BOOK = (True, {"status": "success", "data": []}, 200)


@pytest.fixture
def routing(monkeypatch):
    """Drive the two visible controls: the navbar Analyze toggle and the
    per-strategy ``strategy_mode`` row."""

    state = {"analyze": False, "modes": {}}

    def fake_get_mode(strategy_name):
        mode = state["modes"].get(strategy_name)
        return {"mode": mode} if mode else None

    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: state["analyze"])
    monkeypatch.setattr("database.strategy_mode_db.get_mode", fake_get_mode)
    # Env fall-through must not leak a mode into resolve_mode().
    monkeypatch.delenv("FUTURES_FOLLOW_MODE", raising=False)
    monkeypatch.delenv("SIMPLIFIED_ENGINE_MODE", raising=False)
    # The guard must be on for these to mean anything.
    monkeypatch.setenv("POSITION_RECONCILE_ENABLED", "true")
    # socketio is not initialised under pytest.
    monkeypatch.setattr(
        "services.openposition_service.socketio.start_background_task",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "database.auth_db.get_auth_token_broker",
        lambda *a, **kw: ("auth-token", "zerodha"),
    )
    monkeypatch.setattr(
        "services.openposition_service.get_auth_token_broker",
        lambda *a, **kw: ("auth-token", "zerodha"),
    )
    return state


def _reconcile(mode_key=None, journaled=_QTY, side="SELL"):
    """Run the real reconcile chain, stubbing only the two position STORES."""
    with (
        patch(
            "services.sandbox_service.sandbox_get_positions", return_value=_SANDBOX_BOOK
        ) as sandbox,
        patch(
            "services.positionbook_service.get_positionbook", return_value=_EMPTY_LIVE_BOOK
        ) as live_book,
        patch.object(recon, "_emit_drift_alert") as alert,
    ):
        decision = recon.reconcile_exit(
            strategy="futures_follow_cap50",
            mode_key=mode_key,
            api_key="k",
            symbol=_SYMBOL,
            exchange="NFO",
            product="NRML",
            expected_close_side=side,
            journaled_qty=journaled,
        )
    return decision, sandbox, live_book, alert


# --------------------------------------------------------------------------- #
# The regression — this is the test that fails on the pre-fix tree
# --------------------------------------------------------------------------- #
def test_sandbox_strategy_exit_reconciles_against_sandbox_book(routing):
    """#507: Analyze OFF + strategy_mode='sandbox' MUST reconcile against the
    sandbox book and let the exit PROCEED at the full journalled qty.

    Pre-fix the read fell through to ``resolve_effective_mode()`` (LIVE whenever
    Analyze is off), hit the empty broker book, and the guard SUPPRESSED the
    exit — the exact 2026-07-17..08-07 production behaviour.
    """
    routing["analyze"] = False
    routing["modes"]["futures_follow_cap50"] = "sandbox"

    decision, sandbox, live_book, alert = _reconcile(mode_key="futures_follow_cap50")

    sandbox.assert_called_once()
    live_book.assert_not_called()
    assert decision.action == recon.ACTION_PROCEED
    assert decision.guarded_qty == _QTY
    assert decision.broker_qty == _QTY
    alert.assert_not_called()


def test_missing_mode_key_reproduces_the_pre_fix_suppression(routing):
    """Pin the defect itself: with no ``mode_key`` the read still resolves
    through the analyze overlay, reads the LIVE book, and suppresses a real
    exit. This is the shape #507 describes — it must stay visible so a future
    caller that forgets ``mode_key`` fails a test rather than a trading day."""
    routing["analyze"] = False
    routing["modes"]["futures_follow_cap50"] = "sandbox"

    decision, sandbox, live_book, _alert = _reconcile(mode_key=None)

    live_book.assert_called_once()
    sandbox.assert_not_called()
    assert decision.action == recon.ACTION_SUPPRESS
    assert decision.reason == recon.REASON_BROKER_FLAT
    assert decision.guarded_qty == 0


def test_missing_mode_key_warns(routing, caplog):
    """A missing mode_key on an exit path is logged loudly — silence is how
    this survived three weeks."""
    routing["analyze"] = False
    routing["modes"]["futures_follow_cap50"] = "sandbox"

    with caplog.at_level("WARNING"):
        _reconcile(mode_key=None)

    assert any("no mode_key" in r.message for r in caplog.records)


def test_live_strategy_exit_reconciles_against_broker_book(routing):
    """A genuinely live strategy still reconciles against the real broker book."""
    routing["analyze"] = False
    routing["modes"]["futures_follow_cap50"] = "live"

    decision, sandbox, live_book, _alert = _reconcile(mode_key="futures_follow_cap50")

    live_book.assert_called_once()
    sandbox.assert_not_called()
    # Broker genuinely flat → SUPPRESS is CORRECT here (phantom protection).
    assert decision.action == recon.ACTION_SUPPRESS


def test_analyze_toggle_forces_sandbox_even_for_a_live_strategy(routing):
    """Analyze ON is the platform kill switch — it outranks the strategy row."""
    routing["analyze"] = True
    routing["modes"]["futures_follow_cap50"] = "live"

    decision, sandbox, live_book, _alert = _reconcile(mode_key="futures_follow_cap50")

    sandbox.assert_called_once()
    live_book.assert_not_called()
    assert decision.action == recon.ACTION_PROCEED


def test_unknown_strategy_defaults_to_sandbox(routing):
    """Default deny: no strategy_mode row → sandbox, matching resolve_order_mode."""
    routing["analyze"] = False

    _decision, sandbox, live_book, _alert = _reconcile(mode_key="never_configured")

    sandbox.assert_called_once()
    live_book.assert_not_called()


def test_sandbox_partial_still_clamps(routing):
    """The guard's clamp behaviour is preserved — routing changed, policy did not."""
    routing["analyze"] = False
    routing["modes"]["futures_follow_cap50"] = "sandbox"

    decision, _sandbox, _live, _alert = _reconcile(
        mode_key="futures_follow_cap50", journaled=_QTY + 65
    )

    assert decision.action == recon.ACTION_CLAMP
    assert decision.guarded_qty == _QTY


def test_mode_key_is_forwarded_to_the_positionbook(routing):
    """The live branch must hand ``mode_key`` down, so the two layers cannot
    disagree about which book they are reading."""
    routing["analyze"] = False
    routing["modes"]["futures_follow_cap50"] = "live"

    _decision, _sandbox, live_book, _alert = _reconcile(mode_key="futures_follow_cap50")

    assert live_book.call_args.kwargs.get("mode_key") == "futures_follow_cap50"


# --------------------------------------------------------------------------- #
# Callers pass their canonical key — the half the service cannot enforce
# --------------------------------------------------------------------------- #
def test_simplified_engine_passes_the_dispatch_key_not_the_webhook_label():
    """The engine's ``strategy`` is a webhook LABEL; reusing it as ``mode_key``
    would resolve to an unknown strategy, default-deny to sandbox, and read the
    wrong book on a live engine. It must pass the canonical dispatch key."""
    from services import simplified_stock_engine_service as ses

    with patch.object(recon, "reconcile_exit") as rec:
        ses._reconcile_store_close(
            mode="live",
            strategy="chartink_FnO_intraday_buy",  # webhook label, NOT a mode key
            api_key="k",
            symbol="NBCC",
            exchange="NSE",
            product="MIS",
            expected_close_side="SELL",
            journaled_qty=500,
        )

    assert rec.call_args.kwargs["mode_key"] == "simplified_engine"
    assert rec.call_args.kwargs["strategy"] == "chartink_FnO_intraday_buy"
