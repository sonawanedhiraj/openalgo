"""Replay of a missed open15 session (issue #600).

Two things are being defended here, and they are different in kind:

1. **Fidelity** — the reconstruction must reproduce what the live strategy
   actually selected on a day it really ran. The fixture is 2026-08-14's own
   decision log, so these tests fail if the engine drifts from live behaviour.
2. **Production isolation** — replay must be incapable of touching the real
   money this strategy trades. Those tests assert the guards, not the maths.

The fixture carries only each symbol's 09:15 bar, which is everything selection
needs (gap = 09:15 open / prev close) and keeps the file small. Trigger overlap
is deliberately NOT asserted for equality: it is the ~60%-fidelity part of the
reconstruction, so pinning it would encode noise as a contract.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from services import open15_replay as R

FIXTURE = Path(__file__).parent / "fixtures" / "open15_replay_2026-08-14.json"


@pytest.fixture(scope="module")
def day():
    return json.loads(FIXTURE.read_text())


def _config(day, **over):
    """2026-08-14's armed config: long_only + shadow shorts, OI filter at 500."""
    cfg = {
        "vol_mult": 1.5,
        "top_n": 3,
        "trade_side": "long_only",
        "shadow_side": "S",
        "shadow_max_trades": 3,
        "max_trades": 3,
        "margin_effective": 60000.0,
        "instrument": "atm_option",
        "rolling_enabled": False,  # isolates SEED selection from the rolling add-ons
        "rolling_cadence_s": 30,
        "rolling_top_n": 3,
        "no_entry_after": "09:29",
        "exit_time": "09:30",
        "option_min_oi_lots": 500,
        "excluded": [],
        "config_source": "test",
    }
    cfg.update(over)
    return cfg


def _contracts(day):
    """Contract table in the shape ``resolve_contracts_and_oi`` returns."""
    return {
        key: {
            "symbol": day["opt_symbols"][key],
            "lotsize": 1,
            "oi_lots": lots,
            "bars": {},
        }
        for key, lots in day["oi_lots"].items()
    }


# --------------------------------------------------------------------------- #
# Fidelity
# --------------------------------------------------------------------------- #
def test_seed_selection_matches_the_live_decision_log(day):
    """The whole fidelity claim: same picks, same sides, same gaps as live."""
    run = R.run_core(day["date"], _config(day), day["bars"], day["prev_closes"], _contracts(day))

    assert run["selected"] == day["live_selection"]
    for sym, gap in day["live_gaps_pct"].items():
        assert run["gaps"][sym] == pytest.approx(gap, abs=0.011)


def test_oi_verdicts_match_the_live_stage3_exclusions(day):
    """The #595 filter must block exactly the names live blocked, at the same lots."""
    run = R.run_core(day["date"], _config(day), day["bars"], day["prev_closes"], _contracts(day))

    got = sorted({(e["symbol"], e["side"], e["oi_lots"]) for e in run["oi_exclusions"]})
    # live also blocked MAXHEALTH, but only as a ROLLING addition — the rolling
    # list is off here, so compare on the seed-reachable subset.
    seed_blocks = [tuple(b) for b in day["live_oi_blocks"] if b[0] != "MAXHEALTH"]
    assert got == sorted(seed_blocks)


def test_oi_read_from_the_0915_bar_not_0916(day):
    """The single most breakable detail in the feature (issue #600).

    A bar's ``oi`` is stamped at the END of its minute, so the 09:15 bar is what
    the live 09:16:02 quote sees. MFSL is the canary: 418 lots on the 09:15 bar
    (blocked, as live did) versus 791 on the 09:16 bar (would clear the floor).
    Reading the wrong bar let NMDC through on 2026-08-17 and manufactured a
    phantom +Rs17,924 on a Rs1.67 put.
    """
    series = {
        "MFSL25AUG261560CE": {
            "09:15": {"open": 42.6, "close": 43.0, "volume": 1, "oi": 418},
            "09:16": {"open": 43.0, "close": 43.2, "volume": 1, "oi": 791},
        }
    }
    captured = {}

    def fake_fetch(sym, date):
        captured["sym"] = sym
        return [{"timestamp": ts, **bar} for ts, bar in _epochs(series[sym], date)]

    import services.open15_option_shadow as shadow

    orig_fetch, orig_resolve = shadow._fetch_1m_bars, shadow.resolve_atm_option
    shadow._fetch_1m_bars = fake_fetch
    shadow.resolve_atm_option = lambda *a, **k: {"symbol": "MFSL25AUG261560CE", "lotsize": 1}
    try:
        out = R.resolve_contracts_and_oi(
            "2026-08-14", [("MFSL", "L")], {"MFSL": {"09:15": {"close": 1563.3}}}
        )
    finally:
        shadow._fetch_1m_bars, shadow.resolve_atm_option = orig_fetch, orig_resolve

    assert out["MFSL|L"]["oi_lots"] == 418.0, "OI must come from the 09:15 bar"


def _epochs(bars_by_minute, date):
    """(epoch, bar) pairs for HH:MM keys on ``date``, IST -> UTC."""
    d = dt.date.fromisoformat(date)
    for hhmm, bar in bars_by_minute.items():
        h, m = (int(x) for x in hhmm.split(":"))
        naive = dt.datetime.combine(d, dt.time(h, m)) - R._IST_OFFSET
        yield int(naive.replace(tzinfo=dt.UTC).timestamp()), bar


def test_unknown_oi_fails_open(day):
    """No verdict must never mean 'thin' — the live filter fails open (#390/#555)."""
    contracts = _contracts(day)
    for v in contracts.values():
        v["oi_lots"] = None
    run = R.run_core(day["date"], _config(day), day["bars"], day["prev_closes"], contracts)

    assert run["oi_exclusions"] == []
    # with nothing blocked, the pre-promotion picks come back
    assert "MFSL" in run["selected"]


def test_oi_filter_disabled_when_floor_is_zero(day):
    assert R.make_oi_filter(_contracts(day), 0) is None


# --------------------------------------------------------------------------- #
# Production isolation (G1-G7)
# --------------------------------------------------------------------------- #
def test_g1_replay_imports_no_order_path():
    """Replay must be structurally incapable of placing an order."""
    src = Path(R.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "place_order",
        "order_placer",
        "place_smart_order",
        "close_position",
        "order_router",
    ):
        assert forbidden not in src, f"replay must not reference {forbidden}"


def test_g3_not_imported_by_anything_that_boots():
    """Replay must add no boot-time cost, no scheduler job and no thread.

    Importing the live open15 service and its blueprint — everything the app
    wires up for this strategy — must not drag the replay engine in with them.
    """
    import subprocess
    import sys

    probe = (
        "import sys;"
        "import services.open15_breakout_service, blueprints.open15_breakout;"
        "print('services.open15_replay' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=180)
    assert out.stdout.strip().endswith("False"), out.stdout + out.stderr


def test_g3_registers_no_scheduler_job_or_thread():
    """Code patterns, not prose — the docstring is allowed to SAY 'scheduler'."""
    src = Path(R.__file__).read_text(encoding="utf-8")
    for forbidden in ("add_job(", "Thread(", "BackgroundScheduler", "start_background"):
        assert forbidden not in src, f"replay must not use {forbidden}"


def test_g2_replay_pnl_is_excluded_from_real_aggregates():
    """Without this, replay money compounds into tomorrow's real position size."""
    from database.open15_breakout_db import NON_REAL_FILLS

    assert "replay" in NON_REAL_FILLS


def test_g2_total_realized_pnl_ignores_replay_rows(tmp_path, monkeypatch):
    import database.open15_breakout_db as db

    _rebind(db, tmp_path, monkeypatch)
    db.insert_trade(
        trade_date="2026-08-11",
        symbol="A",
        side="L",
        status="closed",
        fill="real",
        pnl=1000.0,
        charges_inr=100.0,
    )
    before = db.total_realized_pnl()
    db.insert_trade(
        trade_date="2026-08-12",
        symbol="B",
        side="L",
        status="closed",
        fill="replay",
        pnl=50000.0,
        charges_inr=100.0,
    )

    assert db.total_realized_pnl() == before == 900.0
    assert db.replay_pnl_by_date() == {"2026-08-12": 49900.0}
    assert "2026-08-12" not in db.trades_pnl_by_date()


def test_g4_replay_refuses_to_overwrite_a_traded_day(tmp_path, monkeypatch):
    """The #597 clobber class — the guard runs again inside the writer."""
    import database.open15_breakout_db as db

    _rebind(db, tmp_path, monkeypatch)
    monkeypatch.setattr(R, "_early_net", lambda row: None)
    db.insert_trade(
        trade_date="2026-08-13", symbol="REAL", side="L", status="closed", fill="real", pnl=1.0
    )

    rows = [{"status": "closed", "symbol": "X", "side": "L", "pnl": 5.0, "charges_inr": 0.0}]
    with pytest.raises(R.ReplayIneligible) as e:
        R.persist("2026-08-13", {"instrument": "atm_option"}, rows, [])

    assert e.value.reason == "day_was_traded"
    assert _count(db, "2026-08-13") == 1  # nothing was written


def test_g4_has_real_fill_fails_closed(monkeypatch):
    """An unreadable journal must BLOCK the rewrite, never wave it through."""
    import database.open15_breakout_db as db

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db.db_session, "query", boom)
    assert db.has_real_fill("2026-08-12") is True


def test_g6_pre_existing_rows_still_classify_as_real(tmp_path, monkeypatch):
    """Adding a fill class must not reinterpret a single existing row."""
    import database.open15_breakout_db as db

    _rebind(db, tmp_path, monkeypatch)
    db.insert_trade(
        trade_date="2026-08-10",
        symbol="OLD",
        side="L",
        status="closed",
        pnl=200.0,
        charges_inr=20.0,
    )  # fill IS NULL, pre-dates the column

    assert db.trades_pnl_by_date() == {"2026-08-10": 180.0}
    assert db.total_realized_pnl() == 180.0


def test_delete_replay_rows_cannot_touch_other_buckets(tmp_path, monkeypatch):
    import database.open15_breakout_db as db

    _rebind(db, tmp_path, monkeypatch)
    for fill in ("real", "paper", "sim", "shadow", "replay", None):
        db.insert_trade(
            trade_date="2026-08-12", symbol=str(fill), side="L", status="closed", fill=fill, pnl=1.0
        )

    assert db.delete_replay_rows("2026-08-12") == 1
    assert _count(db, "2026-08-12") == 5


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("events", "real_fill", "expect_ok", "reason"),
    [
        ([{"event": "skipped_late_boot"}], False, True, "skipped_late_boot"),
        ([{"event": "no_ticks_received"}], False, True, "no_ticks_received"),
        ([], False, True, "no_day_log"),
        ([{"event": "armed"}, {"event": "summary"}], False, False, "day_ran_normally"),
        ([{"event": "skipped_late_boot"}], True, False, "day_was_traded"),
        ([{"event": "replay_meta"}], False, False, "already_replayed"),
    ],
)
def test_eligibility(monkeypatch, events, real_fill, expect_ok, reason):
    import database.open15_breakout_db as db
    import services.data_freshness_service as fresh

    monkeypatch.setattr(db, "get_day_log", lambda d: events)
    monkeypatch.setattr(db, "has_real_fill", lambda d: real_fill)
    monkeypatch.setattr(fresh, "is_trading_day", lambda d, exchange=None: True)
    # a Sunday evening: a trading DATE is being replayed, outside market hours
    monkeypatch.setattr(R, "_now_ist", lambda: dt.datetime(2026, 8, 16, 20, 0))

    out = R.check_eligibility("2026-08-12")
    assert out["eligible"] is expect_ok
    assert out["reason"] == reason


def test_market_hours_block(monkeypatch):
    """Replay makes ~250 historical calls against the live strategy's quota."""
    import database.open15_breakout_db as db
    import services.data_freshness_service as fresh

    monkeypatch.setattr(db, "get_day_log", lambda d: [{"event": "skipped_late_boot"}])
    monkeypatch.setattr(db, "has_real_fill", lambda d: False)
    monkeypatch.setattr(fresh, "is_trading_day", lambda d, exchange=None: True)
    monkeypatch.setattr(R, "_now_ist", lambda: dt.datetime(2026, 8, 17, 10, 30))

    assert R.check_eligibility("2026-08-12")["reason"] == "market_hours"


def _rebind(db, tmp_path, monkeypatch):
    """Point the module's engine at a throwaway DB for this test."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'open15.db'}")
    session = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "db_session", session)
    db.Base.metadata.create_all(bind=engine)


def _count(db, date: str) -> int:
    return db.db_session.query(db.Open15Trade).filter(db.Open15Trade.trade_date == date).count()
