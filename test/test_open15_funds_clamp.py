"""The day's slot budget must fit the ACCOUNT, not just the config (issue #626).

On 2026-08-18 open15 was configured for 5 slots x Rs60,000 = Rs3,00,000 against
Rs1,22,252.80 of cash. The first two entries filled; the third asked for
Rs62,000 that was not there and Zerodha refused it — "Margin required:
149255.00" being the CUMULATIVE requirement of all three orders, not the third
alone. Nothing in the strategy had ever read the balance.

The clamp shrinks `max_trades`, never `margin_per_slot`: cutting the slot would
silently change the position size this deployment exists to measure and make
the day incomparable to every day before it.
"""

import pytest

from services.open15_breakout_service import clamp_slots_to_funds


def test_the_2026_08_18_budget_is_clamped_to_what_the_account_held():
    """The incident, in one assertion: 5 slots configured, 2 affordable."""
    eff, note = clamp_slots_to_funds(60_000, 5, 122_252.80)

    assert eff == 2, "Rs1,22,252 buys two Rs60,000 slots, not five"
    assert note and "2 of 5 slots" in note


def test_a_funded_account_is_not_clamped():
    eff, note = clamp_slots_to_funds(60_000, 5, 500_000)

    assert eff == 5 and note is None


def test_an_exactly_affordable_budget_is_left_alone():
    """The boundary: 5 x 60,000 == 300,000 is affordable, not one short."""
    eff, note = clamp_slots_to_funds(60_000, 5, 300_000)

    assert eff == 5 and note is None


def test_an_unreadable_balance_fails_open():
    """A transient funds-API failure must not switch the strategy off.

    None means "unknown", never zero. The broker still enforces the real limit,
    and since #626 a rejection is handled correctly rather than published as a
    fill — so failing open costs a rejection, while failing closed costs the day.
    """
    eff, note = clamp_slots_to_funds(60_000, 5, None)

    assert eff == 5 and note is None


def test_an_account_that_cannot_afford_one_slot_trades_nothing():
    eff, note = clamp_slots_to_funds(60_000, 5, 40_000)

    assert eff == 0
    assert note and "0 of 5 slots" in note


def test_a_nonsense_slot_size_is_not_divided_by():
    eff, note = clamp_slots_to_funds(0, 5, 100_000)

    assert eff == 5 and note is None


@pytest.mark.parametrize(
    ("cash", "expected"),
    [(59_999, 0), (60_000, 1), (179_999, 2), (180_000, 3)],
)
def test_the_clamp_floors_rather_than_rounds(cash, expected):
    """Rounding up would reintroduce the exact rejection this prevents."""
    assert clamp_slots_to_funds(60_000, 5, cash)[0] == expected


# --------------------------------------------------------------------------- #
# The funds read must follow the BOOK THE ORDERS GO TO (the #497 rule)
#
# `funds_service.get_funds` dispatches on `resolve_effective_mode()` — the
# ANALYZE OVERLAY, which returns LIVE whenever the navbar toggle is off, no
# matter what this strategy is set to. open15 defaults to sandbox, so reading
# funds through it would size a virtual-Rs1Cr measurement run against the real
# broker balance and clamp a run that has no funding constraint at all. That is
# the same class of bug as #497, where a sandbox strategy read the empty LIVE
# position book and fired no exits for four trading days.
# --------------------------------------------------------------------------- #
def _stub_modes(monkeypatch, order_mode, *, sandbox_cash=None, broker_cash=None):
    import sys

    from services.mode_service import EffectiveMode

    monkeypatch.setattr(
        "services.mode_service.resolve_order_mode", lambda _key: order_mode, raising=False
    )
    monkeypatch.setitem(
        sys.modules,
        "database.auth_db",
        type(
            "M",
            (),
            {
                "get_first_available_api_key": staticmethod(lambda: "k"),
                "get_auth_token_broker": staticmethod(lambda _k: ("tok", "zerodha")),
            },
        ),
    )
    called = {}

    def _sandbox_funds(api_key, original):
        called["sandbox"] = True
        return True, {"data": {"availablecash": sandbox_cash}}, 200

    def _broker_funds(auth_token, broker, original_data=None):
        called["broker"] = True
        return True, {"data": {"availablecash": broker_cash}}, 200

    monkeypatch.setitem(
        sys.modules,
        "services.sandbox_service",
        type("M", (), {"sandbox_get_funds": staticmethod(_sandbox_funds)}),
    )
    monkeypatch.setattr("services.funds_service.get_funds_with_auth", _broker_funds, raising=False)
    return called, EffectiveMode


def test_a_sandbox_strategy_reads_the_sandbox_book_not_the_broker_balance(monkeypatch):
    """The regression: open15 runs in sandbox by default, with Analyze OFF."""
    from services.mode_service import EffectiveMode
    from services.open15_breakout_service import read_available_cash

    called, _ = _stub_modes(
        monkeypatch, EffectiveMode.SANDBOX, sandbox_cash="10000000.00", broker_cash="122252.80"
    )
    cash = read_available_cash()

    assert cash == 10_000_000.00, "the virtual Rs1Cr book, not the real balance"
    assert called.get("sandbox") and not called.get("broker")


def test_a_live_strategy_reads_the_real_broker_balance(monkeypatch):
    from services.mode_service import EffectiveMode
    from services.open15_breakout_service import read_available_cash

    called, _ = _stub_modes(
        monkeypatch, EffectiveMode.LIVE, sandbox_cash="10000000.00", broker_cash="122252.80"
    )
    cash = read_available_cash()

    assert cash == 122_252.80
    assert called.get("broker") and not called.get("sandbox")


def test_a_sandbox_run_is_not_clamped_by_the_real_accounts_shortfall(monkeypatch):
    """End-to-end of the same point, at the decision the clamp actually makes.

    2026-08-18's real balance would cut 5 slots to 2. A sandbox run must keep
    all 5 — its book is not what ran out of money.
    """
    from services.mode_service import EffectiveMode
    from services.open15_breakout_service import clamp_slots_to_funds, read_available_cash

    _stub_modes(
        monkeypatch, EffectiveMode.SANDBOX, sandbox_cash="10000000.00", broker_cash="122252.80"
    )
    eff, note = clamp_slots_to_funds(60_000, 5, read_available_cash())

    assert eff == 5 and note is None
