"""Per-grade long/short configuration (issue #762).

#726 let the operator pick which R63 grades place orders; this makes the SIDE
configurable per grade too (e.g. A both, B long only, C off). The grade is
only known at the trigger while ``trade_side`` decides at selection which
sides are watched, so the per-grade rule is enforced in ``_enter`` through the
same shadow seam as ``rating_excluded``.

Pins:

- the pure resolver: parse/format, legacy derivation from ``trade_grades`` +
  ``trade_side`` (an install that never saves ``grade_sides`` is unchanged),
  partial input keeps the legacy value for unmentioned grades, garbage and
  all-off fall back, ``grade_side_allows`` fails open when ungraded;
- day-config resolution (stored row, env seed, derived ``trade_grades``, the
  ``grade_sides_blocked`` warning);
- the ``_enter`` gate: A short and B long trade; B short and C are
  paper-traded as full-slot shadow rows with no order and no slot; the #721
  ceiling still wins; ungraded still trades;
- the armed event / status block carry the policy;
- config storage roundtrip and the /api/config save (all-off refused).
"""

import pytest

from services.open15_breakout_service import Open15BreakoutService, resolve_day_config
from services.open15_rating import (
    grade_side_allows,
    grade_sides_all_off,
    grade_sides_blocked,
    grades_traded,
    legacy_grade_sides,
    normalize_grade_sides,
    parse_grade_sides,
)

DATE = "2026-10-12"
POLICY = "A:LS,B:L,C:-"


# --------------------------------------------------------------------------- #
# pure resolver
# --------------------------------------------------------------------------- #
class TestResolver:
    def test_legacy_matches_the_726_filter_for_every_combination(self):
        for tg in ("ABC", "AB", "A", "C", None):
            for ts in ("both", "long_only", "short_only"):
                gs = legacy_grade_sides(tg, ts)
                grades = tg or "ABC"
                for g in "ABC":
                    for side in "LS":
                        expected = (
                            g in grades
                            and side
                            in {
                                "both": "LS",
                                "long_only": "L",
                                "short_only": "S",
                            }[ts]
                        )
                        assert grade_side_allows(gs, g, side) is expected, (tg, ts, g, side)

    def test_parse_and_format(self):
        assert parse_grade_sides(POLICY) == {"A": "LS", "B": "L", "C": ""}
        assert normalize_grade_sides(POLICY) == POLICY
        assert normalize_grade_sides("a:sl, b:long ,c:off") == POLICY
        assert normalize_grade_sides({"A": "both", "B": ["L"], "C": "-"}) == POLICY
        assert normalize_grade_sides("A:S") == "A:S,B:LS,C:LS"

    def test_partial_input_keeps_the_legacy_value_for_other_grades(self):
        # trade_grades "AB" + long_only => legacy A:L,B:L,C:-; only C is set
        assert normalize_grade_sides("C:S", "AB", "long_only") == "A:L,B:L,C:S"

    def test_garbage_and_all_off_fall_back_to_legacy(self):
        legacy = legacy_grade_sides("AB", "both")
        assert normalize_grade_sides(None, "AB") == legacy
        assert normalize_grade_sides("nonsense", "AB") == legacy
        assert normalize_grade_sides("A:-,B:-,C:-", "AB") == legacy
        assert normalize_grade_sides("A:xyz", "AB") == legacy

    def test_allows_fails_open_when_ungraded_or_side_unknown(self):
        assert grade_side_allows(POLICY, None, "S")
        assert grade_side_allows(POLICY, "C", None)
        assert grade_side_allows(POLICY, "A", "S")
        assert not grade_side_allows(POLICY, "B", "S")
        assert grade_side_allows(POLICY, "B", "L")
        assert not grade_side_allows(POLICY, "C", "L")

    def test_grades_traded_and_blocked(self):
        assert grades_traded(POLICY) == "AB"
        assert grade_sides_blocked(POLICY, "both") == []
        assert grade_sides_blocked(POLICY, "long_only") == ["A short"]
        assert grade_sides_blocked(POLICY, "short_only") == ["A long", "B long"]

    def test_all_off_detection_is_explicit(self):
        assert grade_sides_all_off("A:-,B:-,C:-")
        assert grade_sides_all_off("A:,B:off,C:none")
        assert grade_sides_all_off({"A": "", "B": "-", "C": []})
        assert not grade_sides_all_off("A:-,B:-")  # C unmentioned
        assert not grade_sides_all_off("A:-,B:L,C:-")
        assert not grade_sides_all_off("garbage")


class TestDayConfig:
    def test_unset_is_derived_and_unchanged(self, monkeypatch):
        monkeypatch.delenv("OPEN15_GRADE_SIDES", raising=False)
        monkeypatch.delenv("OPEN15_TRADE_GRADES", raising=False)
        dc = resolve_day_config({"trade_side": "long_only", "trade_grades": "AB"}, 0.0)
        assert dc["grade_sides"] == "A:L,B:L,C:-"
        assert dc["trade_grades"] == "AB"
        assert dc["grade_sides_blocked"] == []

    def test_stored_row_wins_and_trade_grades_is_derived(self, monkeypatch):
        monkeypatch.setenv("OPEN15_GRADE_SIDES", "A:L,B:L,C:L")
        dc = resolve_day_config({"grade_sides": POLICY, "trade_grades": "ABC"}, 0.0)
        assert dc["grade_sides"] == POLICY
        assert dc["trade_grades"] == "AB"

    def test_env_seed(self, monkeypatch):
        monkeypatch.setenv("OPEN15_GRADE_SIDES", POLICY)
        assert resolve_day_config({}, 0.0)["grade_sides"] == POLICY

    def test_blocked_warning(self, monkeypatch):
        monkeypatch.delenv("OPEN15_GRADE_SIDES", raising=False)
        dc = resolve_day_config({"grade_sides": POLICY, "trade_side": "long_only"}, 0.0)
        assert dc["grade_sides_blocked"] == ["A short"]


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_journal():
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()
    yield
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()


def _mk_service(cfg=None, median=-0.1):
    orders = []

    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = Open15BreakoutService(order_placer=placer)
    svc.day_status = "armed"
    svc._log_date = DATE
    svc.day_config = resolve_day_config(
        {"instrument": "stock", "trade_side": "both", "grade_sides": POLICY, **(cfg or {})},
        0.0,
    )
    svc.positions = {}
    svc._entry_never_filled = lambda _s, _p: False
    svc._reconcile_and_log = lambda **_kw: None
    svc._persist_day_log = lambda: None
    svc.universe_median = lambda: (median, 150)
    return svc, orders


def _action(symbol, side, ratio=1.5, minute="09:18"):
    return {
        "symbol": symbol,
        "side": side,
        "price": 100.0,
        "gap_pct": 2.0 if side == "L" else -2.0,
        "level": 99.0 if side == "L" else 101.0,
        "baseline_vol": 1000.0,
        "cum_vol_at_trigger": 1000.0 * ratio,
        "trigger_minute": minute,
        "trigger_second": 5,
        "watch_source": "seed",
        "shadow": False,
    }


def _events(svc, kind):
    return [e for e in svc.day_log if e.get("event") == kind]


def _rows():
    from database.open15_breakout_db import Open15Trade, db_session

    rows = {
        r.symbol: {"fill": r.fill, "reason": r.reason, "rating": r.rating, "side": r.side}
        for r in db_session.query(Open15Trade).all()
    }
    db_session.remove()
    return rows


class TestEntryGate:
    def test_a_short_trades(self):
        svc, orders = _mk_service()
        svc._enter(_action("ASHORT", "S", minute="09:18"))  # A
        assert len(orders) == 1
        assert _rows()["ASHORT"]["fill"] == "real"
        assert _rows()["ASHORT"]["rating"] == "A"

    def test_b_long_trades(self):
        svc, orders = _mk_service()
        svc._enter(_action("BLONG", "L", minute="09:23"))  # late -> B
        assert len(orders) == 1
        assert _rows()["BLONG"] == {"fill": "real", "reason": None, "rating": "B", "side": "L"}

    def test_b_short_is_paper_traded_no_order_no_slot(self):
        svc, orders = _mk_service()
        svc._enter(_action("BSHORT", "S", minute="09:23"))
        assert orders == []
        ev = _events(svc, "entry_shadow")
        assert len(ev) == 1
        assert ev[0]["reason"] == "rating_excluded" and ev[0]["rating_excluded"] is True
        assert ev[0]["rating"] == "B" and ev[0]["qty"] > 0
        assert "short side" in ev[0]["note"]
        row = _rows()["BSHORT"]
        assert row["fill"] == "shadow" and row["reason"] == "rating_excluded"
        n_real, _p, _s, n_shadow = svc._count_fills()
        assert (n_real, n_shadow) == (0, 1)

    def test_c_is_paper_on_both_sides(self):
        svc, orders = _mk_service()
        svc._enter(_action("CLONG", "L", minute="09:25"))
        svc._enter(_action("CSHORT", "S", minute="09:25"))
        assert orders == []
        rows = _rows()
        assert rows["CLONG"]["fill"] == rows["CSHORT"]["fill"] == "shadow"

    def test_ceiling_still_decides_first(self, monkeypatch):
        monkeypatch.setenv("OPEN15_SIM_SKIPPED_ENABLED", "true")
        svc, orders = _mk_service({"max_vol_ratio": 1.7})
        svc._enter(_action("CAPBS", "S", minute="09:23", ratio=2.6))
        assert orders == []
        assert _events(svc, "entry_shadow") == []
        assert _events(svc, "entry_skipped")[0]["reason"] == "vol_ratio_cap"

    def test_ungraded_trigger_still_trades(self, monkeypatch):
        import services.open15_breakout_service as mod

        def boom(*_a, **_k):
            raise RuntimeError("grader exploded")

        monkeypatch.setattr(mod, "grade_trigger", boom)
        svc, orders = _mk_service({"grade_sides": "A:L,B:-,C:-"})
        svc._enter(_action("UNG", "S", minute="09:25"))
        assert len(orders) == 1

    def test_hand_built_config_without_grade_sides_uses_trade_grades(self):
        svc, orders = _mk_service()
        svc.day_config = {k: v for k, v in svc.day_config.items() if k != "grade_sides"}
        svc.day_config["trade_grades"] = "AB"
        svc._enter(_action("BSH", "S", minute="09:23"))  # B, both sides via legacy
        svc._enter(_action("CSH", "S", minute="09:25"))  # C -> excluded
        assert len(orders) == 1

    def test_status_block_carries_the_policy(self):
        svc, _orders = _mk_service()
        st = svc.rating_status()
        assert st["grade_sides"] == POLICY and st["trade_grades"] == "AB"


class TestConfigStorage:
    def test_roundtrip(self):
        from database.open15_breakout_db import get_config, save_config

        assert save_config(60000.0, "fixed", 1.5, grade_sides=POLICY, trade_grades="AB")
        cfg = get_config()
        assert cfg["grade_sides"] == POLICY and cfg["trade_grades"] == "AB"
        assert save_config(60000.0, "fixed", 1.5, grade_sides=None)
        assert get_config()["grade_sides"] is None

    def test_api_config_saves_and_refuses_all_off(self, monkeypatch):
        from flask import Flask

        import blueprints.open15_breakout as bp
        import database.open15_breakout_db as db
        import utils.session as sess

        monkeypatch.setattr(sess, "is_session_valid", lambda: True)
        saved = {}

        def fake_save(*_args, **kwargs):
            saved.clear()
            saved.update(kwargs)
            return True

        monkeypatch.setattr(db, "get_config", lambda: None)
        monkeypatch.setattr(db, "save_config", fake_save)
        app = Flask(__name__)
        app.register_blueprint(bp.open15_bp)
        app.config["TESTING"] = True
        client = app.test_client()
        url = "/open15_vol_breakout/api/config"

        r = client.post(url, json={"trade_side": "both", "grade_sides": "A:LS,B:L,C:-"})
        assert r.status_code == 200, r.data
        assert saved["grade_sides"] == POLICY
        assert saved["trade_grades"] == "AB"  # kept in step for pre-#762 readers

        r = client.post(url, json={"grade_sides": ""})
        assert r.status_code == 200
        assert saved["grade_sides"] is None

        saved.clear()
        r = client.post(url, json={"grade_sides": "A:-,B:-,C:-"})
        assert r.status_code == 400
        assert "grade sides" in r.get_json()["errors"][0]
        assert saved == {}

        r = client.get(url)
        assert r.status_code == 200
        assert "grade_sides" in r.get_json()["env_defaults"]


def test_logs_page_has_the_grade_side_matrix():
    """The form, the effective line and the provisional chip all read grade_sides."""
    import blueprints.open15_breakout as bp

    src = open(bp.__file__, encoding="utf-8").read()
    assert 'id="c_grmatrix"' in src
    assert "grade_sides:gsFromForm()" in src
    assert "gsAllows(gradeSides(),g,r.side)" in src
