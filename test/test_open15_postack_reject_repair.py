"""One-shot repair for post-ACK broker rejections (issue #626).

The corrupt rows carry a P&L computed from LIMIT prices — neither the quote
estimate the strategy observed nor a real fill — so the repair cannot simply
clear a flag. It rebuilds the quote-derived P&L a paper row is supposed to
carry, then hands the row back to the FIXED reconciliation path to make the
demotion decision, so repair and live behaviour cannot drift apart.

The day log is the other half. `/logs` renders the DECISION LOG, not the
journal: an `entry` left at ``order_status='success'`` keeps the symbol in the
entered count and its `exit` event keeps publishing the P&L chip, however
correct the journal row is (the lesson #548 learned the same way).
"""

import pytest

from services.open15_postack_reject_repair import _quote_derived_pnl, _repair_day_log


class _Row:
    """The journal fields the pricing reads."""

    def __init__(self, **kw):
        defaults = {
            "quantity": 800,
            "instrument": "option",
            "side": "L",
            "opt_entry_premium": 73.8,
            "opt_exit_premium": 89.9,
            "trigger_price": None,
            "exit_price": None,
        }
        defaults.update(kw)
        for key, value in defaults.items():
            setattr(self, key, value)


@pytest.fixture(autouse=True)
def _clean_day_logs():
    from database.open15_breakout_db import Open15DayLog, db_session, init_db

    init_db()
    db_session.query(Open15DayLog).delete()
    db_session.commit()
    db_session.remove()
    yield
    db_session.query(Open15DayLog).delete()
    db_session.commit()
    db_session.remove()


def test_the_option_quote_pnl_is_rebuilt_from_the_premiums_actually_observed():
    """TIINDIA's real numbers: 73.80 in, 89.90 out, 800 qty."""
    gross, charges = _quote_derived_pnl(_Row())

    assert gross == pytest.approx(12880.0), "(89.90 - 73.80) * 800"
    assert charges and charges > 0


def test_a_short_stock_row_prices_in_the_right_direction():
    row = _Row(instrument="stock", side="S", trigger_price=103.0, exit_price=100.0, quantity=100)
    gross, _ = _quote_derived_pnl(row)

    assert gross == pytest.approx(300.0), "a short gains when the price falls"


def test_a_row_missing_a_leg_price_is_not_guessed():
    """No exit premium means no honest paper price — better None than invented."""
    assert _quote_derived_pnl(_Row(opt_exit_premium=None)) == (None, None)
    assert _quote_derived_pnl(_Row(quantity=0)) == (None, None)


def _day_log():
    return [
        {"ts": "09:10:00.000", "event": "armed", "universe": 5},
        {
            "ts": "09:19:58.000",
            "event": "entry",
            "symbol": "TIINDIA",
            "qty": 800,
            "premium": 73.8,
            "contract": "TIINDIA25AUG262800CE",
            "instrument": "option",
            "order_status": "success",
            "order_id": "260818190112881",
        },
        {"ts": "09:19:59.000", "event": "entry", "symbol": "DIXON", "order_status": "success"},
        {
            "ts": "09:30:04.000",
            "event": "exit",
            "symbol": "TIINDIA",
            "qty": 800,
            "gross": 12880.0,
            "charges": 635.4,
            "pnl": 12244.6,
        },
        {"ts": "09:30:05.000", "event": "exit", "symbol": "DIXON", "pnl": 4169.7},
    ]


def _save(date, events):
    from database.open15_breakout_db import save_day_log

    assert save_day_log(date, events)


def test_the_day_log_stops_showing_the_rejection_as_an_entry_and_a_fill():
    from database.open15_breakout_db import get_day_log

    _save("2026-08-18", _day_log())
    _repair_day_log("2026-08-18", {"TIINDIA"}, apply=True)
    events = get_day_log("2026-08-18")

    rejected = [e for e in events if e["event"] == "entry_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["symbol"] == "TIINDIA" and rejected[0]["post_ack"] is True
    assert rejected[0]["slot_released"] is True

    kinds = {(e["event"], e.get("symbol")) for e in events}
    assert ("exit_paper", "TIINDIA") in kinds, "nothing was sold — this is a paper close"
    assert ("exit", "TIINDIA") not in kinds
    assert ("exit", "DIXON") in kinds, "a genuine fill is untouched"


def test_the_repaired_log_makes_the_page_count_the_row_as_paper():
    """The end-to-end point: the digest the page renders must move."""
    from database.open15_breakout_db import get_day_log
    from services.open15_log_view import summarize_day

    _save("2026-08-18", _day_log())
    before = summarize_day("2026-08-18", get_day_log("2026-08-18"))
    assert (before["entered"], before["paper"]) == (2, 0)

    _repair_day_log("2026-08-18", {"TIINDIA"}, apply=True)
    after = summarize_day("2026-08-18", get_day_log("2026-08-18"))
    assert (after["entered"], after["paper"]) == (1, 1)


def test_a_dry_run_writes_nothing():
    from database.open15_breakout_db import get_day_log

    _save("2026-08-18", _day_log())
    out = _repair_day_log("2026-08-18", {"TIINDIA"}, apply=False)

    assert "would write" in out
    assert [e["event"] for e in get_day_log("2026-08-18")] == [e["event"] for e in _day_log()]


def test_repairing_twice_is_a_no_op():
    """Idempotent: an operator re-run must not stack a second rejection event."""
    from database.open15_breakout_db import get_day_log

    _save("2026-08-18", _day_log())
    _repair_day_log("2026-08-18", {"TIINDIA"}, apply=True)
    first = get_day_log("2026-08-18")

    assert _repair_day_log("2026-08-18", {"TIINDIA"}, apply=True) == "already repaired"
    assert get_day_log("2026-08-18") == first
