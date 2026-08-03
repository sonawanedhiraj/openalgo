"""Tests for the intraday_pullback_top2 operator trade-side gate (issue #509).

`trade_side` ∈ both | long_only | short_only decides which BOOK may run. The
load-bearing property under test: because the two books are mutually exclusive
by the 09:30 NIFTY day gate, excluding a side means the strategy does NOT TRADE
on the days that side would have run — it never silently switches to the other
book, and it never selects, watches or journals the excluded side.

Hermetic: fake price/prev-close providers + a recording placer, mirroring
test_intraday_pullback_service.py. Journal writes hit the per-process temp DB
via conftest isolation.
"""

import datetime as dt

import pytest

from services.intraday_pullback_core import (
    TRADE_SIDES,
    PullbackConfig,
    trade_side_allows,
)
from services.intraday_pullback_service import (
    IntradayPullbackService,
    _env_trade_side,
)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
D = dt.date(2026, 1, 5)  # a Monday


class RecordingPlacer:
    def __init__(self):
        self.calls = []

    def __call__(self, mode, order):
        self.calls.append((mode, dict(order)))
        return {"status": "success", "orderid": f"OID{len(self.calls)}"}


def _mk_service(*, direction="up", trade_side="both", placer=None):
    """A service primed for a clean up-day (long book) or down-day (short book).

    up:   NIFTY +0.5%, sector +0.4%, AAA +1.5% (inside the long band [1.0,2.5))
    down: NIFTY -0.5%, sector -0.4%, AAA -3.5% (inside the deep-loser band (-5,-3])
    """
    sector_map = {"AAA": "IDX1", "BBB": "IDX1"}
    prev = {"AAA": 100.0, "BBB": 100.0, "IDX1": 100.0, "NIFTY": 100.0}
    if direction == "up":
        prices = {"NIFTY": 100.5, "IDX1": 100.4, "AAA": 101.5, "BBB": 100.2}
    else:
        prices = {"NIFTY": 99.5, "IDX1": 99.6, "AAA": 96.5, "BBB": 99.8}
    svc = IntradayPullbackService(
        mode="sandbox",
        sector_map=sector_map,
        prev_close_provider=lambda syms, as_of: {s: prev.get(s) for s in syms},
        price_provider=lambda sym, as_of: prices.get(sym),
        bars_provider=lambda sym, as_of: [],
        order_placer=placer or RecordingPlacer(),
        notifier=lambda m: None,
        broker_session_checker=lambda: True,
        now=lambda: dt.datetime.combine(D, dt.time(9, 40), IST),
    )
    from dataclasses import replace

    svc.cfg = replace(svc.cfg, trade_side=trade_side)
    return svc


def _select(svc):
    svc.run_selection(dt.datetime.combine(D, dt.time(9, 30), IST))


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trade_side", "side", "expected"),
    [
        ("both", "L", True),
        ("both", "S", True),
        ("long_only", "L", True),
        ("long_only", "S", False),
        ("short_only", "L", False),
        ("short_only", "S", True),
    ],
)
def test_trade_side_allows_matrix(trade_side, side, expected):
    assert trade_side_allows(trade_side, side) is expected


@pytest.mark.parametrize("bad", [None, "", "LONG", "longs", "nonsense", "both "])
def test_trade_side_allows_fails_open_on_unknown(bad):
    """An unrecognised value must never dark a book — fail open to 'both'."""
    assert trade_side_allows(bad, "L") is True
    assert trade_side_allows(bad, "S") is True


def test_default_config_is_both():
    """Default = the backtested configuration."""
    assert PullbackConfig().trade_side == "both"
    assert set(TRADE_SIDES) == {"both", "long_only", "short_only"}


# ---------------------------------------------------------------------------
# Env / config-snapshot default resolution
# ---------------------------------------------------------------------------


def test_env_trade_side_defaults_to_both(monkeypatch):
    monkeypatch.delenv("INTRADAY_PULLBACK_TRADE_SIDE", raising=False)
    assert _env_trade_side({}) == "both"


def test_env_trade_side_reads_env(monkeypatch):
    monkeypatch.setenv("INTRADAY_PULLBACK_TRADE_SIDE", "long_only")
    assert _env_trade_side({}) == "long_only"


def test_env_trade_side_env_beats_config_snapshot(monkeypatch):
    monkeypatch.setenv("INTRADAY_PULLBACK_TRADE_SIDE", "short_only")
    assert _env_trade_side({"trade_side": "long_only"}) == "short_only"


def test_env_trade_side_falls_back_to_config_snapshot(monkeypatch):
    monkeypatch.delenv("INTRADAY_PULLBACK_TRADE_SIDE", raising=False)
    assert _env_trade_side({"trade_side": "long_only"}) == "long_only"


def test_env_trade_side_invalid_falls_back_to_both(monkeypatch):
    """A typo must not silently dark a book."""
    monkeypatch.setenv("INTRADAY_PULLBACK_TRADE_SIDE", "LONGS")
    assert _env_trade_side({}) == "both"


def test_env_trade_side_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("INTRADAY_PULLBACK_TRADE_SIDE", "Long_Only")
    assert _env_trade_side({}) == "long_only"


# ---------------------------------------------------------------------------
# Selection gate — the behaviour that matters
# ---------------------------------------------------------------------------


def test_both_selects_long_on_up_day():
    svc = _mk_service(direction="up", trade_side="both")
    _select(svc)
    assert svc.side == "L"
    assert svc.picks == ["AAA"]
    assert svc.skip_reason is None


def test_both_selects_short_on_down_day():
    svc = _mk_service(direction="down", trade_side="both")
    _select(svc)
    assert svc.side == "S"
    assert svc.picks == ["AAA"]
    assert svc.skip_reason is None


def test_long_only_still_trades_the_up_day():
    svc = _mk_service(direction="up", trade_side="long_only")
    _select(svc)
    assert svc.side == "L"
    assert svc.picks == ["AAA"]
    assert svc.skip_reason is None


def test_long_only_skips_the_down_day_entirely():
    """The load-bearing case: a down day under long_only is a NO-TRADE day.

    It must NOT fall back to running the long book on a down day — the day gate
    is part of what R53 validated.
    """
    svc = _mk_service(direction="down", trade_side="long_only")
    _select(svc)
    assert svc.picks == []
    assert svc.states == {}
    assert svc.selected is True  # the day is settled, not left pending
    assert svc.skip_reason == "trade_side=long_only"


def test_short_only_still_trades_the_down_day():
    svc = _mk_service(direction="down", trade_side="short_only")
    _select(svc)
    assert svc.side == "S"
    assert svc.picks == ["AAA"]
    assert svc.skip_reason is None


def test_short_only_skips_the_up_day_entirely():
    svc = _mk_service(direction="up", trade_side="short_only")
    _select(svc)
    assert svc.picks == []
    assert svc.states == {}
    assert svc.selected is True
    assert svc.skip_reason == "trade_side=short_only"


def test_excluded_side_places_no_orders_and_journals_nothing():
    """An excluded side is never watched, so no eval tick can produce an order."""
    placer = RecordingPlacer()
    svc = _mk_service(direction="down", trade_side="long_only", placer=placer)
    _select(svc)
    for hh, mm in ((9, 40), (10, 0), (13, 30), (14, 30)):
        svc.run_eval_tick(dt.datetime.combine(D, dt.time(hh, mm), IST))
    assert placer.calls == []
    assert svc.open_positions == {}


# ---------------------------------------------------------------------------
# Observability — a deliberate skip must not look like a data outage
# ---------------------------------------------------------------------------


def test_status_surfaces_trade_side_and_skip_reason():
    svc = _mk_service(direction="down", trade_side="long_only")
    _select(svc)
    st = svc.get_status()
    assert st["trade_side"] == "long_only"
    assert st["skip_reason"] == "trade_side=long_only"


def test_entry_breakdown_names_the_gate():
    svc = _mk_service(direction="down", trade_side="long_only")
    _select(svc)
    bd = svc.entry_breakdown()
    assert bd["trade_side"] == "long_only"
    assert bd["skip_reason"] == "trade_side=long_only"
    assert bd["picks"] == []


def test_current_settings_exposes_trade_side():
    svc = _mk_service(trade_side="short_only")
    assert svc.current_settings()["trade_side"] == "short_only"


def test_daily_reset_clears_skip_reason():
    """Yesterday's deliberate skip must not leak into today's status."""
    svc = _mk_service(direction="down", trade_side="long_only")
    _select(svc)
    assert svc.skip_reason == "trade_side=long_only"
    svc._reset_state()
    assert svc.skip_reason is None


# ---------------------------------------------------------------------------
# DB round-trip
# ---------------------------------------------------------------------------


def test_config_db_round_trips_trade_side():
    """The column persists and reads back (conftest redirects to a temp DB)."""
    from database.intraday_pullback_config_db import (
        delete_config,
        get_config,
        init_db,
        set_config,
    )

    init_db()
    name = "intraday_pullback_top2"
    try:
        set_config(name, updated_by="test", base_capital=60000, trade_side="short_only")
        assert get_config(name)["trade_side"] == "short_only"
        # An update of an unrelated field must not clobber the stored side.
        set_config(name, updated_by="test", base_capital=75000)
        row = get_config(name)
        assert row["trade_side"] == "short_only"
        assert row["base_capital"] == 75000
    finally:
        delete_config(name)


def test_config_db_reset_reverts_to_default():
    """Deleting the row (Reset to defaults) drops the override entirely."""
    from database.intraday_pullback_config_db import (
        delete_config,
        get_config,
        init_db,
        set_config,
    )

    init_db()
    name = "intraday_pullback_top2"
    set_config(name, updated_by="test", trade_side="long_only")
    assert get_config(name)["trade_side"] == "long_only"
    delete_config(name)
    assert get_config(name) is None


def test_service_merges_stored_trade_side_over_default(monkeypatch):
    """A persisted row wins over the env/JSON default at _apply_editable_config."""
    from database.intraday_pullback_config_db import delete_config, init_db, set_config

    init_db()
    name = "intraday_pullback_top2"
    monkeypatch.delenv("INTRADAY_PULLBACK_TRADE_SIDE", raising=False)
    try:
        set_config(name, updated_by="test", trade_side="short_only")
        svc = _mk_service()
        svc._apply_editable_config()
        assert svc.cfg.trade_side == "short_only"
    finally:
        delete_config(name)


def test_service_ignores_invalid_stored_trade_side(monkeypatch):
    """Garbage in the row must not dark a book — keep the env/JSON default."""
    from database.intraday_pullback_config_db import delete_config, init_db, set_config

    init_db()
    name = "intraday_pullback_top2"
    monkeypatch.delenv("INTRADAY_PULLBACK_TRADE_SIDE", raising=False)
    try:
        set_config(name, updated_by="test", trade_side="nonsense")
        svc = _mk_service()
        svc._apply_editable_config()
        assert svc.cfg.trade_side == "both"
    finally:
        delete_config(name)


# ---------------------------------------------------------------------------
# Blueprint validation
# ---------------------------------------------------------------------------


_VALID_WINDOWS = {
    "base_capital": 60000,
    "sizing_mode": "fixed",
    "no_trade_start": "11:00",
    "no_trade_end": "13:00",
    "afternoon_start": "13:00",
    "afternoon_end": "15:00",
}


@pytest.fixture
def client(monkeypatch):
    from flask import Flask

    import blueprints.intraday_pullback as bp

    monkeypatch.setattr(bp, "_authed_for_read", lambda: True)
    monkeypatch.setattr(bp, "_service_or_503", lambda: (_mk_service(), None))

    app = Flask(__name__)
    app.register_blueprint(bp.intraday_pullback_bp)
    app.config["TESTING"] = True
    return app.test_client()


def test_settings_post_rejects_bad_trade_side(client):
    r = client.post(
        "/intraday_pullback_top2/api/settings",
        json={**_VALID_WINDOWS, "trade_side": "longs"},
    )
    assert r.status_code == 400
    assert "trade_side" in r.get_json()["message"]


def test_settings_post_saves_valid_trade_side(client, monkeypatch):
    import database.intraday_pullback_config_db as cdb

    saved = {}
    monkeypatch.setattr(cdb, "set_config", lambda name, updated_by="ui", **f: saved.update(f) or {})
    r = client.post(
        "/intraday_pullback_top2/api/settings",
        json={**_VALID_WINDOWS, "trade_side": "long_only"},
    )
    assert r.status_code == 200
    assert saved["trade_side"] == "long_only"


def test_settings_post_without_trade_side_leaves_it_alone(client, monkeypatch):
    """An API-key caller updating only capital must not reset the operator's side."""
    import database.intraday_pullback_config_db as cdb

    saved = {}
    monkeypatch.setattr(cdb, "set_config", lambda name, updated_by="ui", **f: saved.update(f) or {})
    r = client.post("/intraday_pullback_top2/api/settings", json=dict(_VALID_WINDOWS))
    assert r.status_code == 200
    assert "trade_side" not in saved


def test_settings_get_exposes_trade_side(client):
    r = client.get("/intraday_pullback_top2/api/settings")
    assert r.status_code == 200
    assert r.get_json()["data"]["trade_side"] == "both"
