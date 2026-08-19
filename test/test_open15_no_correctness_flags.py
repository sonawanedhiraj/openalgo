"""Correctness has no off switch (issue #651).

Four `OPEN15_*` flags used to gate behaviour with no legitimate second setting:

    OPEN15_VERIFY_ENTRIES          off -> publish a broker rejection as a fill
    OPEN15_CONFIRM_EXIT_POSITION   off -> square off on belief, not on the book
    OPEN15_FILL_RECONCILE_ENABLED  off -> report quote P&L, not the broker's
    OPEN15_FUNDS_CLAMP             off -> size a budget without reading the balance

Each shipped as a rollback switch in case the fix itself was wrong — a
shipping-time concern with a shelf life. Kept past it, a guarantee becomes a
preference, and this codebase has already paid for that: #647's contract-existence
FACT sat behind the liquidity gate's `enabled` flag, so switching off a percentile
JUDGEMENT switched off the fact too, and SAMMAANCAP traded into a dead end with
the verdict recorded `enforced: false`.

Deleting a flag is easy to undo by accident — a future "let me make this
configurable while I debug it" is exactly how they appeared the first time. These
tests fail if any of the four comes back.

They do NOT test the behaviours themselves; those keep their own suites
(`test_open15_rejected_entry`, `test_open15_fill_reconcile`,
`test_open15_funds_clamp`), and #651 removed the env setup from them rather than
the coverage. What is asserted here is only that the SWITCH is gone.
"""

import subprocess

import pytest

RETIRED = (
    "OPEN15_VERIFY_ENTRIES",
    "OPEN15_CONFIRM_EXIT_POSITION",
    "OPEN15_FILL_RECONCILE_ENABLED",
    "OPEN15_FUNDS_CLAMP",
)


@pytest.mark.parametrize("flag", RETIRED)
def test_no_module_reads_the_retired_flag(flag):
    """The structural half: nothing may consult these again.

    A grep rather than a behavioural assertion on purpose — a re-introduced flag
    would most likely appear in a NEW guard somewhere else, which no existing
    test would cover.
    """
    hits = subprocess.run(
        ["git", "grep", "-n", flag, "--", "services/", "blueprints/", "database/", "app.py"],
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert not hits, f"{flag} was retired by #651 but is read again:\n{hits}"


@pytest.mark.parametrize("flag", RETIRED)
def test_setting_the_retired_flag_to_false_is_inert(flag, monkeypatch):
    """Belt and braces: even set to the value that used to disable the guard,
    the code paths still exist and still run."""
    monkeypatch.setenv(flag, "false")

    import importlib

    import services.open15_breakout_service as svc_mod
    import services.open15_fill_reconcile as fr_mod

    importlib.reload(fr_mod)

    # the helpers themselves are gone
    for name in (
        "_verify_entries_enabled",
        "_confirm_exit_position",
        "_fill_reconcile_enabled",
        "_funds_clamp_enabled",
    ):
        assert not hasattr(svc_mod, name), f"{name} came back"
    assert not hasattr(fr_mod, "_enabled"), "the fill-reconcile flag came back"


def test_the_funds_clamp_still_reads_the_balance_with_the_flag_off(monkeypatch):
    """The 2026-08-18 incident in one assertion, with the old kill switch set.

    5 slots x Rs60,000 against Rs1,22,252.80 of cash: the clamp must still cut
    it to 2 whatever the retired flag says.
    """
    monkeypatch.setenv("OPEN15_FUNDS_CLAMP", "false")
    from services.open15_breakout_service import clamp_slots_to_funds

    eff, note = clamp_slots_to_funds(60_000, 5, 122_252.80)

    assert eff == 2 and note and "2 of 5 slots" in note


def test_arm_reads_the_balance_unconditionally(monkeypatch):
    """`read_available_cash()` used to be skipped entirely when the flag was off
    AND residual sizing was off — so the `armed` event stamped no
    `available_cash`, and the capital card had nothing to render."""
    import datetime as dtm

    import database.open15_breakout_db as db
    import services.open15_breakout_service as svc_mod
    from services.open15_breakout_service import IST, Open15BreakoutService

    db.init_db()
    monkeypatch.setenv("OPEN15_FUNDS_CLAMP", "false")  # inert since #651
    watched = {f"SYM{i}" for i in range(25)}
    reads = []

    monkeypatch.setattr(
        Open15BreakoutService,
        "_now_ist",
        staticmethod(lambda: IST.localize(dtm.datetime(2026, 8, 19, 9, 10, 0))),
    )
    monkeypatch.setattr(Open15BreakoutService, "_load_universe", staticmethod(lambda: set(watched)))
    monkeypatch.setattr(
        Open15BreakoutService,
        "_load_prev_closes",
        lambda self, universe, date: dict.fromkeys(universe, 100.0),
    )
    monkeypatch.setattr(svc_mod, "fetch_broker_prev_closes", lambda universe: {})
    monkeypatch.setattr(svc_mod, "verify_prev_closes", lambda prev, date: (prev, {}))
    monkeypatch.setattr(svc_mod, "read_available_cash", lambda: (reads.append(1), 161_365.10)[1])
    monkeypatch.setattr(
        Open15BreakoutService, "_apply_liquidity_stage1", lambda self, today: (None, [])
    )
    monkeypatch.setattr(Open15BreakoutService, "_build_oi_filter", lambda self, now: None)
    monkeypatch.setattr(Open15BreakoutService, "_apply_exit_schedule", lambda self: None)

    svc = Open15BreakoutService(order_placer=lambda mode, order: {"status": "success"})
    svc.arm()

    assert reads, "the balance must be read even with the retired flag set to false"
    armed = next(e for e in svc.day_log if e["event"] == "armed")
    assert armed["available_cash"] == 161_365.10
