"""Tests for the UI-configurable open15 trade side (issue #503).

Covers: the env default, day-config resolution (row -> env -> default, bad
value falls back), the core's selection gate (the whole enforcement point —
an excluded side is never selected, so it is never watched and never enters),
the DB round-trip for the new column, and the blueprint's enum validation.
"""

import os

os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "0" * 64)

import datetime as dt  # noqa: E402

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from services.open15_breakout_service import (  # noqa: E402
    TRADE_SIDES,
    Open15Core,
    _trade_side_default,
    resolve_day_config,
)


def t(h, m, s=0):
    return dt.datetime(2026, 7, 21, h, m, s)


def build_core(trade_side):
    """Core with one clear gainer (AAA) and one clear loser (ZZZ)."""
    core = Open15Core({"AAA": 100.0, "ZZZ": 100.0}, vol_mult=1.5, top_n=3, trade_side=trade_side)
    for sym, open_px in (("AAA", 102.0), ("ZZZ", 98.0)):
        core.on_tick(sym, open_px, 400, t(9, 15, 1))
        core.on_tick(sym, open_px, 1000, t(9, 15, 55))
    return core


# ---- env default ---------------------------------------------------------- #


def test_trade_side_default(monkeypatch):
    monkeypatch.delenv("OPEN15_TRADE_SIDE", raising=False)
    assert _trade_side_default() == "both"
    monkeypatch.setenv("OPEN15_TRADE_SIDE", "LONG_ONLY")
    assert _trade_side_default() == "long_only"
    monkeypatch.setenv("OPEN15_TRADE_SIDE", "sideways")
    assert _trade_side_default() == "both"


# ---- day-config resolution ------------------------------------------------ #


def test_resolve_day_config_trade_side_default():
    assert resolve_day_config(None, 0.0)["trade_side"] == "both"


def test_resolve_day_config_trade_side_from_row():
    cfg = resolve_day_config({"trade_side": "short_only"}, 0.0)
    assert cfg["trade_side"] == "short_only"


def test_resolve_day_config_trade_side_invalid_falls_back():
    assert resolve_day_config({"trade_side": "junk"}, 0.0)["trade_side"] == "both"


def test_resolve_day_config_trade_side_env(monkeypatch):
    monkeypatch.setenv("OPEN15_TRADE_SIDE", "long_only")
    assert resolve_day_config(None, 0.0)["trade_side"] == "long_only"
    # an explicit row still wins over the env default
    assert resolve_day_config({"trade_side": "both"}, 0.0)["trade_side"] == "both"


# ---- core selection gate -------------------------------------------------- #


def test_core_both_selects_each_side():
    core = build_core("both")
    core.on_tick("AAA", 102.0, 1200, t(9, 16, 5))
    assert core.selected == {"AAA": "L", "ZZZ": "S"}


def test_core_long_only_drops_shorts():
    core = build_core("long_only")
    core.on_tick("AAA", 102.0, 1200, t(9, 16, 5))
    assert core.selected == {"AAA": "L"}


def test_core_short_only_drops_longs():
    core = build_core("short_only")
    core.on_tick("AAA", 102.0, 1200, t(9, 16, 5))
    assert core.selected == {"ZZZ": "S"}


def test_core_excluded_side_never_enters():
    """A short that would otherwise trigger produces no entry under long_only."""
    core = build_core("long_only")
    core.on_tick("ZZZ", 97.9, 1200, t(9, 16, 5))
    low = core.sym["ZZZ"]["fc"]["low"]
    # breaks the 09:15 low on a big volume surge — would be an entry under "both"
    assert core.on_tick("ZZZ", low - 0.5, 50_000, t(9, 17, 10)) is None
    assert core.entered == {}


def test_core_invalid_trade_side_falls_back_to_both():
    core = Open15Core({"AAA": 100.0}, trade_side="junk")
    assert core.trade_side == "both"


def test_trade_sides_constant():
    assert TRADE_SIDES == ("both", "long_only", "short_only")


# ---- DB round-trip -------------------------------------------------------- #


def test_config_db_roundtrip_trade_side():
    import database.open15_breakout_db as db

    db.init_db()
    assert db.save_config(
        30000.0,
        "fixed",
        1.5,
        updated_by="test",
        instrument="stock",
        max_trades=3,
        trade_side="short_only",
    )
    assert db.get_config()["trade_side"] == "short_only"
    # cleared back to NULL = fall through to the env default
    assert db.save_config(30000.0, "fixed", 1.5, updated_by="test")
    assert db.get_config()["trade_side"] is None


# ---- blueprint validation ------------------------------------------------- #


@pytest.fixture
def client(monkeypatch):
    import utils.session as sess

    monkeypatch.setattr(sess, "is_session_valid", lambda: True)
    import blueprints.open15_breakout as bp

    app = Flask(__name__)
    app.register_blueprint(bp.open15_bp)
    app.config["TESTING"] = True
    return app.test_client()


def test_config_post_rejects_bad_trade_side(client, monkeypatch):
    import database.open15_breakout_db as db

    monkeypatch.setattr(db, "get_config", lambda: None)
    r = client.post("/open15_vol_breakout/api/config", json={"trade_side": "longs"})
    assert r.status_code == 400
    assert any("trade_side" in e for e in r.get_json()["errors"])


def test_config_post_saves_trade_side(client, monkeypatch):
    import database.open15_breakout_db as db

    saved = {}

    def fake_save(*args, **kwargs):
        saved.update(kwargs)
        return True

    monkeypatch.setattr(db, "get_config", lambda: None)
    monkeypatch.setattr(db, "save_config", fake_save)
    r = client.post("/open15_vol_breakout/api/config", json={"trade_side": "long_only"})
    assert r.status_code == 200
    assert saved["trade_side"] == "long_only"


def test_config_get_exposes_trade_side_env_default(client, monkeypatch):
    import database.open15_breakout_db as db

    monkeypatch.setattr(db, "get_config", lambda: None)
    monkeypatch.setenv("OPEN15_TRADE_SIDE", "short_only")
    r = client.get("/open15_vol_breakout/api/config")
    assert r.status_code == 200
    assert r.get_json()["env_defaults"]["trade_side"] == "short_only"
