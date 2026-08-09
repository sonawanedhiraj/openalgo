"""Tests for the open15 option-liquidity gate (issue #583, Gate 1).

The gate is two-stage because the two obvious single-stage designs each break
something: filtering at 09:10 alone means guessing the side, filtering only after the
09:16 ranking lets an illiquid name occupy a top-N slot first.

The load-bearing tests here are:
  - stage 1 drops ONLY both-side failures, so a one-side failure survives to stage 2;
  - stage 2 fires on a ROLLING addition, not just a seed pick — ``maybe_rerank``
    assigns sides independently, so a check placed only in ``_finalize_selection``
    would leave half the watch list ungated;
  - a stage-1 exclusion reaches the rolling path too, via the tick gate rather than
    via a second check;
  - fail OPEN on a data gap, fail CLOSED on a missing contract.
"""

import os

os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "0" * 64)

import datetime as dt  # noqa: E402

import pytest  # noqa: E402

from services.open15_breakout_service import Open15Core, resolve_day_config  # noqa: E402
from services.open15_liquidity_gate import (  # noqa: E402
    REASON_ILLIQUID,
    REASON_NO_CONTRACTS,
    Exclusion,
    LiquidityGate,
    _hysteresis_excluded,
    build_gate,
)


def t(h, m, s=0):
    return dt.datetime(2026, 8, 10, h, m, s)


FIRST_CANDLES = {
    "GOOD": {"open": 102.0, "high": 103.0, "low": 101.0},
    "ONESIDE": {"open": 101.5, "high": 102.0, "low": 101.0},
    "DEAD": {"open": 101.0, "high": 102.0, "low": 100.5},
    "SHORTY": {"open": 98.0, "high": 99.0, "low": 97.0},
}
PREV = dict.fromkeys(FIRST_CANDLES, 100.0)


def _gate(excluded: dict, contracts=None, have_scores=True):
    """``excluded`` maps ``(symbol, 'CE'|'PE')`` -> pctile."""
    g = LiquidityGate(
        enabled=True, min_pctile=20, reentry_pctile=25, reentry_days=3, have_scores=have_scores
    )
    for (sym, side), p in excluded.items():
        g.excluded_sides[(sym, side)] = Exclusion(
            sym, REASON_ILLIQUID, side="long" if side == "CE" else "short", pctile=p
        )
    g.contract_underlyings = set(contracts) if contracts is not None else set()
    return g


# ---------------------------------------------------------------------------
# stage 1 vs stage 2 split
# ---------------------------------------------------------------------------


def test_stage1_drops_only_both_side_failures():
    g = _gate({("DEAD", "CE"): 2.0, ("DEAD", "PE"): 1.0, ("ONESIDE", "CE"): 5.0})
    assert g.stage1("DEAD") is not None
    # ONESIDE fails calls only — it is perfectly tradeable short, so stage 1
    # must NOT drop it. This is the UNOMINDA case (CE p28 / PE p10, inverted).
    assert g.stage1("ONESIDE") is None
    assert g.stage1("GOOD") is None


def test_stage2_catches_the_one_side_failure_on_the_failing_side_only():
    g = _gate({("ONESIDE", "CE"): 5.0})
    assert g.stage2("ONESIDE", "L") is not None  # long buys a CE -> blocked
    assert g.stage2("ONESIDE", "S") is None  # short buys a PE -> fine
    ex = g.stage2("ONESIDE", "L")
    assert ex.reason == REASON_ILLIQUID
    assert ex.side == "long"


def test_stage1_both_side_exclusion_reports_both():
    g = _gate({("DEAD", "CE"): 2.0, ("DEAD", "PE"): 1.0})
    ex = g.stage1("DEAD")
    assert ex.side == "both"
    assert ex.detail["ce_pctile"] == 2.0 and ex.detail["pe_pctile"] == 1.0


# ---------------------------------------------------------------------------
# fail-open vs fail-closed
# ---------------------------------------------------------------------------


def test_data_gap_fails_open():
    """No usable scores must INCLUDE everything. Darkening a universe because a
    collection job failed is the #390 shape."""
    g = _gate({}, contracts={"GOOD"}, have_scores=False)
    assert g.stage1("GOOD") is None
    assert g.stage2("GOOD", "L") is None


def test_missing_contracts_fails_closed_even_with_no_scores():
    """A symbol with no NFO contracts is a stale SCANNER_SYMBOLS, not an illiquidity
    verdict — and it must be excluded even when the score read failed."""
    g = _gate({}, contracts={"GOOD"}, have_scores=False)
    ex = g.stage1("GONE")
    assert ex is not None and ex.reason == REASON_NO_CONTRACTS
    assert g.stage2("GONE", "L").reason == REASON_NO_CONTRACTS


def test_disabled_gate_excludes_nothing():
    g = _gate({("DEAD", "CE"): 1.0, ("DEAD", "PE"): 1.0})
    g.enabled = False
    assert g.stage1("DEAD") is None
    assert g.stage2("DEAD", "L") is None


# ---------------------------------------------------------------------------
# hysteresis
# ---------------------------------------------------------------------------


def test_hysteresis_keeps_a_flapping_name_out():
    """p19 / p21 / p19 must not enter and leave on alternate days."""
    assert _hysteresis_excluded(19.0, [21.0, 19.0], 20, 25, 3) is True
    # back above the floor but still inside the band, with a recent dip -> stays out
    assert _hysteresis_excluded(21.0, [19.0, 21.0], 20, 25, 3) is True


def test_hysteresis_readmits_after_a_clean_window():
    """Above the RE-ENTRY line is immediate; inside the band needs a clean window."""
    assert _hysteresis_excluded(26.0, [19.0, 19.0], 20, 25, 3) is False
    assert _hysteresis_excluded(22.0, [22.0, 23.0], 20, 25, 3) is False


def test_hysteresis_never_excludes_a_missing_median():
    assert _hysteresis_excluded(None, [], 20, 25, 3) is False


# ---------------------------------------------------------------------------
# build_gate: insufficient history
# ---------------------------------------------------------------------------


def test_insufficient_history_is_included_not_excluded(monkeypatch):
    """A newly listed F&O name reports "cannot rank" and is watched. Scoring it low
    would be indistinguishable from scoring it on no evidence."""
    monkeypatch.setattr(
        "services.option_liquidity_service.option_underlyings", lambda: {"NEW", "OLD"}
    )
    monkeypatch.setattr(
        "database.option_liquidity_db.get_latest_scores",
        lambda **kw: {
            ("NEW", "CE"): {"option_liquidity_pctile": 3.0, "n_days_in_median": 4},
            ("NEW", "PE"): {"option_liquidity_pctile": 3.0, "n_days_in_median": 4},
            ("OLD", "CE"): {"option_liquidity_pctile": 3.0, "n_days_in_median": 20},
            ("OLD", "PE"): {"option_liquidity_pctile": 3.0, "n_days_in_median": 20},
        },
    )
    monkeypatch.setattr("database.option_liquidity_db.get_daily_pctile_history", lambda *a: {})
    cfg = resolve_day_config(None, 0.0)
    g = build_gate(cfg, dt.date(2026, 8, 10))
    # ``would_exclude``, not ``stage1``: the shipped default is enforcement OFF, and
    # stage1 honours that. The VERDICT is what this test is about.
    assert g.would_exclude("NEW") is None, "too-new must not be treated as illiquid"
    assert g.would_exclude("OLD") is not None


def test_build_gate_fails_open_when_the_read_raises(monkeypatch):
    monkeypatch.setattr("services.option_liquidity_service.option_underlyings", lambda: {"AAA"})

    def _boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("database.option_liquidity_db.get_latest_scores", _boom)
    g = build_gate(resolve_day_config(None, 0.0), dt.date(2026, 8, 10))
    assert g.have_scores is False
    assert g.would_exclude("AAA") is None, "a failed read must fail OPEN even as a verdict"


# ---------------------------------------------------------------------------
# core integration — seed AND rolling
# ---------------------------------------------------------------------------


def _core(gate, backfill=True, top_n=1, rolling=False):
    return Open15Core(
        dict(PREV),
        top_n=top_n,
        await_snapshot=True,
        trade_side="both",
        rolling_enabled=rolling,
        rolling_cadence_s=10,
        rolling_top_n=2,
        liquidity_gate=gate,
        liquidity_backfill_rank=backfill,
    )


def test_seed_selection_skips_the_excluded_side_and_backfills():
    """GOOD gaps +2%, ONESIDE +1.5%. With top_n=1 and GOOD blocked on calls, the
    slot must go to ONESIDE rather than being lost."""
    c = _core(_gate({("GOOD", "CE"): 4.0}))
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("GOOD", 102.0, 1000, t(9, 16, 1))
    assert c.selected.get("ONESIDE") == "L"
    assert "GOOD" not in c.selected
    assert any(
        e["symbol"] == "GOOD" and e["watch_source"] == "seed" for e in c.liquidity_exclusions
    )


def test_seed_leaves_the_slot_empty_when_backfill_is_off():
    c = _core(_gate({("GOOD", "CE"): 4.0}), backfill=False)
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("GOOD", 102.0, 1000, t(9, 16, 1))
    assert "GOOD" not in c.selected
    assert "ONESIDE" not in c.selected, "backfill off must forgo the slot, not fill it"


def test_stage2_fires_on_a_ROLLING_addition_not_just_a_seed_pick():
    """The regression that would otherwise ship a half-covered gate.

    ``maybe_rerank`` assigns a side at its own site, so a check wired only into
    ``_finalize_selection`` leaves every rolling addition ungated.
    """
    c = _core(_gate({("ONESIDE", "CE"): 4.0}), top_n=1, rolling=True)
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("GOOD", 102.0, 1000, t(9, 16, 1))
    assert c.selected.get("GOOD") == "L"
    c.liquidity_exclusions.clear()
    # ONESIDE now rallies hard — it would be the top rolling gainer
    for sym, px in (("ONESIDE", 130.0), ("GOOD", 103.0)):
        c.on_tick(sym, px, 2000, t(9, 20, 0))
    c.maybe_rerank(t(9, 20, 5))
    assert "ONESIDE" not in c.selected, "an illiquid CE must not be added by the re-rank"
    assert any(
        e["symbol"] == "ONESIDE" and e["watch_source"] == "rolling" for e in c.liquidity_exclusions
    )


def test_rolling_addition_allowed_when_its_own_side_is_fine():
    """SHORTY is blocked on CALLS but the re-rank would add it as a SHORT (PE)."""
    c = _core(_gate({("SHORTY", "CE"): 4.0}), top_n=1, rolling=True)
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("GOOD", 102.0, 1000, t(9, 16, 1))
    for sym, px in (("SHORTY", 70.0), ("GOOD", 103.0)):
        c.on_tick(sym, px, 2000, t(9, 20, 0))
    c.maybe_rerank(t(9, 20, 5))
    assert c.selected.get("SHORTY") == "S", "the PE side is liquid — it must be watchable"


def test_no_gate_is_a_pure_no_op():
    """Deploying with the gate off must leave selection byte-identical."""
    a = _core(None)
    a.apply_first_candles(FIRST_CANDLES)
    a.on_tick("GOOD", 102.0, 1000, t(9, 16, 1))
    assert a.selected.get("GOOD") == "L"
    assert a.liquidity_exclusions == []


def test_a_raising_gate_never_costs_a_selection():
    class _Broken:
        def stage2(self, *a):
            raise RuntimeError("boom")

    c = _core(_Broken())
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("GOOD", 102.0, 1000, t(9, 16, 1))
    assert c.selected.get("GOOD") == "L", "a broken gate must fail OPEN"


@pytest.mark.parametrize("side_cfg,blocked_side", [("long_only", "CE"), ("short_only", "PE")])
def test_gate_respects_trade_side(side_cfg, blocked_side):
    """With one side switched off entirely, only the traded side's score matters."""
    c = Open15Core(
        dict(PREV),
        top_n=1,
        await_snapshot=True,
        trade_side=side_cfg,
        liquidity_gate=_gate({("GOOD", blocked_side): 4.0, ("SHORTY", blocked_side): 4.0}),
    )
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("GOOD", 102.0, 1000, t(9, 16, 1))
    assert "GOOD" not in c.selected or c.selected.get("GOOD") != (
        "L" if blocked_side == "CE" else "S"
    )


# ---------------------------------------------------------------------------
# measure-without-acting (the placebo failed, 2026-08-09)
# ---------------------------------------------------------------------------


def test_disabled_gate_still_records_what_it_would_have_excluded():
    """Enforcement off must NOT switch off the data that could overturn it.

    Replayed against the R60 July option-leg trades, excluding the real bottom
    quintile was indistinguishable from excluding the same number of symbols at
    random (placebo >= real 48.4% / 51.8%), so the gate ships measuring. The
    exclusions still have to be logged or the cohort never accrues evidence.
    """
    g = _gate({("GOOD", "CE"): 4.0})
    g.enabled = False
    c = _core(g, top_n=1)
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("GOOD", 102.0, 1000, t(9, 16, 1))
    assert c.selected.get("GOOD") == "L", "a disabled gate must not change selection"
    rec = [e for e in c.liquidity_exclusions if e["symbol"] == "GOOD"]
    assert rec, "a disabled gate must still RECORD the verdict"
    assert rec[0]["enforced"] is False


def test_enabled_gate_marks_its_exclusions_enforced():
    c = _core(_gate({("GOOD", "CE"): 4.0}), top_n=1)
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("GOOD", 102.0, 1000, t(9, 16, 1))
    assert "GOOD" not in c.selected
    assert all(e["enforced"] is True for e in c.liquidity_exclusions)


def test_gate_default_is_off():
    """The shipped default must be OFF until the placebo is beaten."""
    from services.open15_breakout_service import _liq_gate_enabled_default, resolve_day_config

    assert _liq_gate_enabled_default() is False
    assert resolve_day_config(None, 0.0)["option_liquidity_gate_enabled"] is False


def test_ui_env_defaults_match_the_service_defaults():
    """The config API must not re-derive a default the service already owns.

    Caught in the UI walkthrough: the blueprint read
    ``os.getenv("OPEN15_LIQUIDITY_GATE_ENABLED", "true")`` while the service default
    had moved to false, so the page rendered a TICKED "exclude illiquid option books"
    box for a gate that would never fire. Two encodings of one default is how a UI
    and an engine silently disagree.
    """
    import re
    from pathlib import Path

    src = Path("blueprints/open15_breakout.py").read_text(encoding="utf-8")
    block = src.split('"env_defaults"', 1)[1].split("},", 1)[0]
    offenders = re.findall(r'"(option_(?:liquidity|impact)_\w+)":\s*[^,\n]*getenv', block)
    assert not offenders, (
        f"env_defaults re-derives {offenders} from os.getenv instead of calling the "
        "service default getter — the UI and the engine will drift apart"
    )
