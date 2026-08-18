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
