"""Issue #587 — boot fetch workers gate on master-contract readiness.

Login triggers the daily master-contract download at the same instant the
boot backfill workers see a live broker session, so their first broker
fetches used to race the SymToken swap. ``wait_for_master_contract_ready``
waits out an in-flight download (bounded) and FAILS OPEN everywhere else —
the previous contract is still usable, so a gate problem must never block
the backfills themselves.
"""

import threading

import pytest

import services.broker_session_health as bsh


@pytest.fixture(autouse=True)
def _fast_gate(monkeypatch):
    monkeypatch.setattr(bsh, "_active_broker", lambda: "zerodha")


def _patch_status(monkeypatch, statuses):
    import database.master_contract_status_db as mcs

    seq = iter(statuses)
    calls = []

    def fake_get_status(broker):
        calls.append(broker)
        return {"status": next(seq)}

    monkeypatch.setattr(mcs, "get_status", fake_get_status)
    return calls


def test_waits_through_download_then_returns_true(monkeypatch):
    calls = _patch_status(monkeypatch, ["pending", "downloading", "success"])
    assert bsh.wait_for_master_contract_ready(deadline_sec=5, poll_sec=0.01) is True
    assert calls == ["zerodha", "zerodha", "zerodha"]


def test_error_status_fails_open_immediately(monkeypatch):
    calls = _patch_status(monkeypatch, ["error"])
    assert bsh.wait_for_master_contract_ready(deadline_sec=5, poll_sec=0.01) is False
    assert len(calls) == 1


def test_unknown_status_fails_open_immediately(monkeypatch):
    _patch_status(monkeypatch, ["unknown"])
    assert bsh.wait_for_master_contract_ready(deadline_sec=5, poll_sec=0.01) is False


def test_timeout_fails_open(monkeypatch):
    _patch_status(monkeypatch, ["downloading"] * 100)
    assert bsh.wait_for_master_contract_ready(deadline_sec=0, poll_sec=0.01) is False


def test_status_read_exception_fails_open(monkeypatch):
    import database.master_contract_status_db as mcs

    def boom(broker):
        raise RuntimeError("status db unavailable")

    monkeypatch.setattr(mcs, "get_status", boom)
    assert bsh.wait_for_master_contract_ready(deadline_sec=5, poll_sec=0.01) is False


def test_flag_off_skips_gate_entirely(monkeypatch):
    monkeypatch.setenv("MASTER_CONTRACT_BOOT_GATE_ENABLED", "false")

    def must_not_be_called():
        raise AssertionError("gate disabled — broker lookup must not run")

    monkeypatch.setattr(bsh, "_active_broker", must_not_be_called)
    assert bsh.wait_for_master_contract_ready(deadline_sec=5, poll_sec=0.01) is True


def test_no_active_broker_returns_true(monkeypatch):
    monkeypatch.setattr(bsh, "_active_broker", lambda: None)
    assert bsh.wait_for_master_contract_ready(deadline_sec=5, poll_sec=0.01) is True


def test_stop_event_aborts_wait(monkeypatch):
    _patch_status(monkeypatch, ["downloading"] * 100)
    stop = threading.Event()
    stop.set()
    assert (
        bsh.wait_for_master_contract_ready(deadline_sec=5, poll_sec=0.01, stop_event=stop) is False
    )


# --------------------------------------------------------------------------- #
# Boot-worker wiring: the gate runs between the session wait and the fetches
# --------------------------------------------------------------------------- #


def test_scanner_backfill_boot_worker_calls_gate_before_work(monkeypatch):
    import services.scanner_backfill_scheduler as sbs

    calls = []
    monkeypatch.setattr(sbs, "_wait_for_broker_session", lambda *a, **k: True)
    monkeypatch.setattr(
        bsh, "wait_for_master_contract_ready", lambda **k: calls.append("gate") or True
    )
    monkeypatch.setattr(sbs, "run_boot_backfill_checks", lambda: calls.append("work"))
    monkeypatch.setattr(sbs, "start_periodic_backfill_check", lambda: None)
    monkeypatch.setattr(sbs, "start_straggler_recheck", lambda: None)

    sbs._boot_worker()

    assert calls == ["gate", "work"]


def test_sector_follow_boot_worker_calls_gate_before_work(monkeypatch):
    import services.sector_follow_backfill_scheduler as sfs

    calls = []
    monkeypatch.setattr(sfs, "_wait_for_broker_session", lambda *a, **k: True)
    monkeypatch.setattr(
        bsh, "wait_for_master_contract_ready", lambda **k: calls.append("gate") or True
    )
    monkeypatch.setattr(sfs, "run_boot_backfill_checks", lambda: calls.append("work"))
    monkeypatch.setattr(sfs, "start_periodic_backfill_check", lambda: None)

    sfs._boot_worker()

    assert calls == ["gate", "work"]


def test_aggregator_seeder_boot_worker_calls_gate_before_seed(monkeypatch):
    import services.scanner_aggregator_seeder as seeder

    calls = []
    monkeypatch.setattr(seeder, "_wait_for_broker_session", lambda *a, **k: True)
    monkeypatch.setattr(
        bsh, "wait_for_master_contract_ready", lambda **k: calls.append("gate") or True
    )

    def fake_seed(aggregator, symbols, bar_15m_history=None):
        calls.append("seed")
        return {
            "seeded_symbols": 1,
            "empty_symbols": [],
            "total_bars": 10,
            "avg_bars_per_symbol": 10.0,
            "errors": 0,
            "seeded_15m_bars": 0,
        }

    monkeypatch.setattr(seeder, "seed_aggregator", fake_seed)
    monkeypatch.setattr(seeder, "_notify", lambda msg: None)

    seeder._boot_worker(aggregator=object(), symbols=["SBIN"])

    assert calls == ["gate", "seed"]
