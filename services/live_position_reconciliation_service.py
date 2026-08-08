"""Position-store reconciliation at exit (issue #265).

At exit the *mode-appropriate position store* is the **source of truth**: the
real broker positionbook in ``live`` mode, and the ``sandbox.db`` virtual book in
``sandbox`` mode. This module gives every exit path a single, defensive guard
that reconciles the strategy's journalled/in-memory close quantity against that
store's actual net position **before** the exit order is placed — in BOTH modes.

The position read is routed through
:func:`services.openposition_service.get_open_position`, which is itself
mode-aware: it returns the ``sandbox.db`` position in sandbox mode and the broker
positionbook in live mode. So the guard's semantics are identical in both modes;
only the underlying store differs.

Why this exists
---------------
Both ``futures_follow_cap50`` and ``simplified_engine`` track open positions in
process memory (``paper_book`` / ``engine.positions``) and a write-only trade
journal. Neither consulted the position store at exit, so on a journal↔store
mismatch they could:

* double-SELL a NIFTY future after a manual/partial exit → net SHORT overnight
  (SPAN + gap risk);
* fire a phantom exit when the journal reads open but the store is flat →
  reject, or worse, reverse the position;
* exit the wrong quantity on a partial fill.

The fix is to make the store's net qty authoritative at exit. Given the intended
close, :func:`reconcile_exit` returns a *guarded* decision:

* ``store_qty == 0`` → **SUPPRESS** the exit (phantom); reason ``broker_flat``.
* ``abs(store_qty) < abs(journaled)`` OR the store position sits on the wrong
  side of the intended close → **CLAMP** the close qty to ``abs(store_qty)``;
  reason ``partial_mismatch``. (A store position on the *opposite* side of the
  close means there is nothing to close in the expected direction — clamp to 0,
  i.e. suppress.)
* store qty present and consistent → **PROCEED** with the store qty.
* store fetch fails → **FAIL CLOSED for reverse-risk**: never close more than
  journaled, emit an alert; the caller proceeds with the (unchanged) journalled
  qty rather than an unbounded one. Never raises into the caller.

On ANY mismatch (and on a fetch failure) a position-drift alert is emitted via
:func:`services.source_divergence_alerts.check_and_alert` (labels ``journal_qty``
vs ``broker_qty``, per-(strategy, symbol, day) dedup — the same discipline as the
existing data-drift path).

Design constraints
------------------
* **Both modes, mode-aware store — via ``mode_key``.** The guard runs in ``live``
  AND ``sandbox``; the caller no longer gates on mode. The store it consults is
  chosen by ``resolve_order_mode(mode_key)``, the same resolver that decided
  where the ENTRY was written, so the guard never over-exits against whichever
  store is authoritative for the running mode. **The caller must pass its
  canonical ``mode_key``.** Issue #507: without it the read resolves through the
  platform analyze overlay, which with Analyze OFF returns the LIVE book for
  every strategy — futures_follow_cap50's sandbox position then read flat and
  this guard suppressed its T+1 exit for three weeks.
  :func:`reconcile_exit` short-circuits only when the ``POSITION_RECONCILE_ENABLED``
  flag is off, returning a PROCEED decision with the journalled qty unchanged.
* **Import-light.** All heavy imports are lazy inside the function body so
  importing this module never pulls the order/quote stack.
* **Exception-safe.** Never raises back into the caller — a store/alert failure
  degrades to the fail-closed PROCEED-with-journalled-qty path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from utils.logging import get_logger

logger = get_logger(__name__)

# Guarded-decision actions.
ACTION_PROCEED = "proceed"  # place the exit at ``guarded_qty`` (== journaled)
ACTION_CLAMP = "clamp"  # place the exit at ``guarded_qty`` (< journaled)
ACTION_SUPPRESS = "suppress"  # do NOT place any exit (phantom / broker flat)

# Reasons (carried into logs, alerts, journal notes).
REASON_MATCH = "match"
REASON_BROKER_FLAT = "broker_flat"
REASON_PARTIAL_MISMATCH = "partial_mismatch"
REASON_OPPOSITE_SIDE = "opposite_side"
REASON_BROKER_FETCH_FAILED = "broker_fetch_failed"
REASON_DISABLED = "disabled"


@dataclass
class ReconcileDecision:
    """Guarded close decision returned by :func:`reconcile_exit`.

    Attributes:
        broker_qty: broker net qty (signed) if known, else ``None`` (fetch failed).
        action: one of ``proceed`` / ``clamp`` / ``suppress``.
        guarded_qty: the (non-negative) quantity the caller should actually close.
            ``0`` iff ``action == suppress``.
        reason: short machine reason (one of the ``REASON_*`` constants).
    """

    broker_qty: int | None
    action: str
    guarded_qty: int
    reason: str

    @property
    def should_place(self) -> bool:
        return self.action != ACTION_SUPPRESS


def is_enabled() -> bool:
    """``POSITION_RECONCILE_ENABLED`` env flag (default ``true``).

    The single emergency disable for the whole guard (both modes). When off,
    :func:`reconcile_exit` returns a PROCEED decision with the journalled qty
    unchanged (legacy behaviour).
    """
    return os.getenv("POSITION_RECONCILE_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _fetch_broker_qty(
    api_key: str,
    symbol: str,
    exchange: str,
    product: str,
    mode_key: str | None = None,
) -> tuple[bool, int]:
    """Return ``(ok, signed_net_qty)`` from the mode-appropriate position store.

    ``ok`` is ``False`` on any fetch/parse failure — the caller then fails closed.
    Never raises. Reads via ``openposition_service.get_open_position``.

    ``mode_key`` is what MAKES that read mode-aware (issue #507): it routes the
    lookup through ``resolve_order_mode(mode_key)``, so a sandbox strategy reads
    ``sandbox.db`` and a live one reads the broker positionbook. Without it the
    read falls back to the platform analyze overlay, which with Analyze OFF
    reports the LIVE book for EVERY strategy — a sandbox position then reads
    flat and this guard suppresses a legitimate exit. Callers on a strategy's
    own exit path must always pass their canonical key.
    """
    try:
        from services.openposition_service import get_open_position

        position_data = {"symbol": symbol, "exchange": exchange, "product": product}
        success, resp, _ = get_open_position(position_data, api_key=api_key, mode_key=mode_key)
        if not success or not isinstance(resp, dict):
            logger.warning(
                "live_position_reconcile: broker fetch failed for %s (%s/%s): %r",
                symbol,
                exchange,
                product,
                resp,
            )
            return False, 0
        raw = resp.get("quantity", 0)
        return True, int(float(raw))
    except Exception:
        logger.exception(
            "live_position_reconcile: broker fetch raised for %s (%s/%s)",
            symbol,
            exchange,
            product,
        )
        return False, 0


def _emit_drift_alert(
    strategy: str, symbol: str, journaled_qty: int, broker_qty: int | float
) -> None:
    """Best-effort position-drift alert (journal_qty vs broker_qty). Never raises."""
    try:
        from services.source_divergence_alerts import check_and_alert

        check_and_alert(
            service=strategy,
            symbol=symbol,
            source_a_label="journal_qty",
            source_a_value=float(journaled_qty),
            source_b_label="broker_qty",
            source_b_value=float(broker_qty),
        )
    except Exception:
        logger.exception(
            "live_position_reconcile: drift alert dispatch failed (%s %s)", strategy, symbol
        )


def reconcile_exit(
    *,
    strategy: str,
    api_key: str | None,
    symbol: str,
    exchange: str,
    product: str,
    expected_close_side: str,
    journaled_qty: int,
    mode_key: str | None = None,
) -> ReconcileDecision:
    """Reconcile an exit's close qty against the mode-appropriate store's net position.

    Safe to call in BOTH modes — but ONLY when ``mode_key`` is supplied. The
    underlying read (``openposition_service.get_open_position``) is mode-aware
    *because of* ``mode_key``; without it the read falls back to the platform
    analyze overlay and reports the LIVE book for every strategy (issue #507).
    Returns a :class:`ReconcileDecision` describing whether to proceed, clamp, or
    suppress. Never raises.

    Args:
        strategy: strategy name (dedup key + alert label + log tag). This is a
            LABEL and may be a webhook name — it is deliberately NOT reused as
            ``mode_key``, because an unrecognised label resolves to sandbox
            (default deny) and would silently read the wrong book.
        api_key: OpenAlgo api key used to read the mode-appropriate position store.
            When ``None``/empty the guard fails closed (proceeds with journalled qty).
        symbol: OpenAlgo-format symbol of the position being closed.
        exchange: exchange code (e.g. ``NFO``, ``NSE``).
        product: product code (e.g. ``NRML``, ``MIS``, ``CNC``).
        expected_close_side: the exit action — ``SELL`` closes a long, ``BUY``
            closes a short. Determines which store-position sign is "closeable".
        journaled_qty: the strategy's own (positive) close quantity.
        mode_key: the strategy's CANONICAL order-dispatch key (e.g.
            ``futures_follow_cap50``, ``simplified_engine``). Required in
            practice: it is what routes the read to the same store the entry
            was written to.

    Returns:
        A guarded :class:`ReconcileDecision`.
    """
    journaled = abs(int(journaled_qty))
    side = (expected_close_side or "").strip().upper()

    # A missing mode_key on an exit path IS the #507 defect — the read silently
    # resolves through the analyze overlay. Never guess a key here (a wrong one
    # reads the wrong book just as silently); make the omission loud instead.
    if not mode_key:
        logger.warning(
            "live_position_reconcile[%s]: no mode_key for %s — position read will fall "
            "back to the analyze overlay and may consult the WRONG book (issue #507). "
            "Callers on a strategy exit path must pass their canonical mode_key.",
            strategy,
            symbol,
        )

    # Flag off → legacy behaviour: proceed with journalled qty, no broker call.
    if not is_enabled():
        return ReconcileDecision(
            broker_qty=None,
            action=ACTION_PROCEED,
            guarded_qty=journaled,
            reason=REASON_DISABLED,
        )

    # No api key → cannot consult the broker. Fail closed for reverse-risk: never
    # close MORE than journaled, and alert.
    if not api_key:
        logger.warning(
            "live_position_reconcile[%s]: no api_key for %s — failing closed "
            "(proceeding with journaled qty=%d)",
            strategy,
            symbol,
            journaled,
        )
        _emit_drift_alert(strategy, symbol, journaled, journaled + 1)
        return ReconcileDecision(
            broker_qty=None,
            action=ACTION_PROCEED,
            guarded_qty=journaled,
            reason=REASON_BROKER_FETCH_FAILED,
        )

    ok, broker_qty = _fetch_broker_qty(api_key, symbol, exchange, product, mode_key=mode_key)

    if not ok:
        # FAIL CLOSED for reverse-risk: do NOT exit more than journaled. Proceed
        # with the journalled qty (never an unbounded one) and alert. The alert
        # uses a deliberately-diverging broker value so the drift threshold fires.
        logger.error(
            "live_position_reconcile[%s]: broker fetch FAILED for %s — failing closed "
            "(proceeding with journaled qty=%d, NOT more)",
            strategy,
            symbol,
            journaled,
        )
        _emit_drift_alert(strategy, symbol, journaled, journaled + 1)
        return ReconcileDecision(
            broker_qty=None,
            action=ACTION_PROCEED,
            guarded_qty=journaled,
            reason=REASON_BROKER_FETCH_FAILED,
        )

    # Broker net is the source of truth from here on. Determine the qty that is
    # actually closeable in the intended direction.
    #   - Closing a LONG (SELL): only a POSITIVE broker net is closeable.
    #   - Closing a SHORT (BUY): only a NEGATIVE broker net is closeable.
    if side == "SELL":
        closeable = broker_qty if broker_qty > 0 else 0
    elif side == "BUY":
        closeable = -broker_qty if broker_qty < 0 else 0
    else:
        # Unknown side — be conservative: treat magnitude as closeable so we never
        # exit MORE than the broker holds, but still clamp to it.
        logger.warning(
            "live_position_reconcile[%s]: unknown close side %r for %s — using |broker_qty|",
            strategy,
            expected_close_side,
            symbol,
        )
        closeable = abs(broker_qty)

    # Phantom: broker flat (net 0).
    if broker_qty == 0:
        logger.error(
            "live_position_reconcile[%s]: SUPPRESS exit for %s — broker flat "
            "(journaled=%d, broker=0)",
            strategy,
            symbol,
            journaled,
        )
        _emit_drift_alert(strategy, symbol, journaled, 0)
        return ReconcileDecision(
            broker_qty=0,
            action=ACTION_SUPPRESS,
            guarded_qty=0,
            reason=REASON_BROKER_FLAT,
        )

    # Broker sits on the WRONG side of the intended close — there is nothing to
    # close in the expected direction. Suppress (closing would OPEN/REVERSE).
    if closeable == 0:
        logger.error(
            "live_position_reconcile[%s]: SUPPRESS exit for %s — broker on opposite side "
            "(journaled=%d %s, broker_net=%d)",
            strategy,
            symbol,
            journaled,
            side,
            broker_qty,
        )
        _emit_drift_alert(strategy, symbol, journaled, broker_qty)
        return ReconcileDecision(
            broker_qty=broker_qty,
            action=ACTION_SUPPRESS,
            guarded_qty=0,
            reason=REASON_OPPOSITE_SIDE,
        )

    # Partial mismatch: broker holds fewer than journaled → clamp DOWN to broker.
    if closeable < journaled:
        logger.warning(
            "live_position_reconcile[%s]: CLAMP exit for %s — broker holds %d < journaled %d",
            strategy,
            symbol,
            closeable,
            journaled,
        )
        _emit_drift_alert(strategy, symbol, journaled, broker_qty)
        return ReconcileDecision(
            broker_qty=broker_qty,
            action=ACTION_CLAMP,
            guarded_qty=int(closeable),
            reason=REASON_PARTIAL_MISMATCH,
        )

    # Consistent (broker >= journaled on the closeable side). Close exactly the
    # journalled qty — the strategy never exits MORE than it opened, even if the
    # broker shows a larger unrelated position on the same symbol.
    return ReconcileDecision(
        broker_qty=broker_qty,
        action=ACTION_PROCEED,
        guarded_qty=journaled,
        reason=REASON_MATCH,
    )
