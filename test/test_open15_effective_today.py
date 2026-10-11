"""\"Effective today\" must describe TODAY'S ARM, not the process's boot (#645).

`/api/config` served `svc.day_config` as "effective today". That attribute is
set by `arm()` at 09:10 and reverts to the constructor's env-only defaults on
any restart — so on **2026-08-19**, restarted at 11:33 after the arm, the /logs
line read

    effective today: stock | max trades 3 | margin 30000 ... | rolling watch-list disabled

for a session that had actually run `atm_option`, 2 funded slots of Rs60,000,
and the rolling watch-list on (8 adds). Four fields wrong.

What made it a defect rather than a cosmetic staleness is that #643's capital
card sits two rows below and states the truth — `2 of 3 funded at Rs60,000
each` — so one page asserted two different days. `instrument` was the sharpest
tell: `stock` printed one row above a chip reading `atm_option`.

The `armed` event is the durable record of that snapshot. These tests pin that
it is reshaped faithfully, that the source is always labelled, and that a day
which never armed says so instead of presenting defaults as fact.
"""

import pytest

from services.open15_log_view import effective_from_armed

# the real 2026-08-19 event, trimmed to the fields the page reads
ARMED_2026_08_19 = {
    "ts": "09:11:24.998",
    "event": "armed",
    "universe": 211,
    "vol_mult": 1.5,
    "mode": "live",
    "no_entry_after": "09:29",
    "exit_time": "09:30",
    "trade_side": "both",
    "instrument": "atm_option",
    "rolling_watchlist_enabled": True,
    "rolling_cadence_s": 30,
    "rolling_top_n": 3,
    "shadow_excluded_side": True,
    "shadow_side": None,
    "shadow_max_trades": 3,
    "sizing_mode": "fixed",
    "margin_per_slot": 60000.0,
    "margin_effective": 60000.0,
    "notional": 300000.0,
    "cum_realized_pnl": 0.0,
    "max_trades_configured": 3,
    "max_trades_effective": 2,
    "available_cash": 161365.1,
    "funds_clamp": "funds clamp: Rs161,365 available covers 2 of 3 slots at Rs60,000 each",
    "residual_sizing": False,
    "residual_reserve_pct": 3.0,
    "residual_min_lots": 1,
}


# --------------------------------------------------------------------------- #
# 1. The reshape — every field the line got wrong on 2026-08-19
# --------------------------------------------------------------------------- #
def test_the_armed_event_reshapes_into_what_the_day_actually_ran():
    eff = effective_from_armed(ARMED_2026_08_19)

    # the four fields the page printed wrongly after the restart
    assert eff["instrument"] == "atm_option"
    assert eff["max_trades"] == 2
    assert eff["margin_effective"] == 60000.0
    assert eff["rolling_watchlist_enabled"] is True


def test_the_cap_keeps_both_halves_so_the_line_can_explain_itself():
    """`effMaxTrades` renders "2 (of 3 configured — funds)" from these two."""
    eff = effective_from_armed(ARMED_2026_08_19)

    assert (eff["max_trades_configured"], eff["max_trades_effective"]) == (3, 2)
    assert eff["funds_clamp"].startswith("funds clamp:")


def test_leverage_is_derived_because_the_event_never_recorded_it():
    """Missing != zero. The page prints `e.leverage` — an absent key rendered
    the literal string "undefined" in the middle of the line."""
    eff = effective_from_armed(ARMED_2026_08_19)

    assert eff["leverage"] == 5.0  # 300000 / 60000
    assert "leverage" not in ARMED_2026_08_19, "derived, not passed through"


def test_a_zero_margin_yields_no_leverage_rather_than_a_division_error():
    eff = effective_from_armed({**ARMED_2026_08_19, "margin_effective": 0})

    assert eff.get("leverage") is None


def test_residual_sizing_is_renamed_to_the_key_the_page_reads():
    """`residual_sizing` in the event, `residual_sizing_enabled` in day_config —
    the page reads the latter and would have shown "not spent" on a day that
    spent it."""
    on = effective_from_armed({**ARMED_2026_08_19, "residual_sizing": True})

    assert on["residual_sizing_enabled"] is True
    assert on["residual_reserve_pct"] == 3.0 and on["residual_min_lots"] == 1


@pytest.mark.parametrize("ev", [None, {}, {"event": "selection"}, {"event": "summary"}])
def test_anything_that_is_not_an_armed_event_is_refused(ev):
    """Returning a half-built dict here would be worse than returning nothing:
    the caller falls through to the real config instead of inventing a day."""
    assert effective_from_armed(ev) is None


def test_an_old_armed_event_missing_the_newer_fields_still_reshapes():
    """Days before #626/#643 have no cap split and no residual keys. They must
    not produce KeyErrors — the history sidebar reaches back months."""
    old = {
        "event": "armed",
        "instrument": "stock",
        "margin_effective": 30000.0,
        "notional": 150000.0,
    }
    eff = effective_from_armed(old)

    assert eff["instrument"] == "stock" and eff["leverage"] == 5.0
    assert "max_trades" not in eff and "residual_sizing_enabled" not in eff


# --------------------------------------------------------------------------- #
# 2. The endpoint helper — which source, and is it labelled
# --------------------------------------------------------------------------- #
class _Svc:
    def __init__(self, day_status="idle", log_date=None, day_config=None):
        self.day_status = day_status
        self._log_date = log_date
        self.day_config = day_config or {"instrument": "stock", "max_trades": 3}


def _today():
    import datetime as dt

    import pytz

    return dt.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")


def test_a_live_armed_process_serves_its_own_day_config(monkeypatch):
    from blueprints.open15_breakout import _effective_today

    svc = _Svc("armed", _today(), {"instrument": "atm_option", "max_trades": 2})
    eff, src = _effective_today(svc)

    assert src == "armed" and eff["instrument"] == "atm_option"


def test_a_restart_falls_back_to_the_persisted_armed_event(monkeypatch):
    """The 2026-08-19 case: armed at 09:10, restarted at 11:33."""
    import database.open15_breakout_db as db
    from blueprints.open15_breakout import _effective_today

    monkeypatch.setattr(db, "get_day_log", lambda date: [ARMED_2026_08_19])
    # a fresh process: day_status back to idle, day_config back to env defaults
    svc = _Svc("idle", None, {"instrument": "stock", "max_trades": 3, "margin_effective": 30000})
    eff, src = _effective_today(svc)

    assert src == "armed_log"
    assert eff["instrument"] == "atm_option" and eff["max_trades"] == 2
    assert eff["margin_effective"] == 60000.0


def test_a_day_that_never_armed_says_so_instead_of_asserting_defaults(monkeypatch):
    import database.open15_breakout_db as db
    import services.open15_breakout_service as mod
    from blueprints.open15_breakout import _effective_today

    monkeypatch.setattr(db, "get_day_log", lambda date: None)
    monkeypatch.setattr(mod, "load_arm_config", lambda: {"instrument": "atm_option"})
    eff, src = _effective_today(_Svc("idle"))

    # labelled as a preview, and the preview is the ARM's resolution (#764)
    assert src == "not_armed" and eff["instrument"] == "atm_option"


def test_the_not_armed_preview_reads_the_saved_row_not_env_defaults(monkeypatch):
    """issue #764 - the 2026-10-11 case. The process had not armed, so
    ``svc.day_config`` was the constructor's env-only snapshot; the line read
    `stock | margin 30000 | grades A both · B both · C both` under a form
    showing the saved ATM option / 60000 / A-only row."""
    import database.open15_breakout_db as db
    from blueprints.open15_breakout import _effective_today
    from services.open15_breakout_service import resolve_day_config

    saved = {
        "margin_per_slot": 60000.0,
        "sizing_mode": "fixed",
        "vol_mult": 1.5,
        "instrument": "atm_option",
        "trade_side": "both",
        "trade_grades": "A",
    }
    monkeypatch.setattr(db, "get_day_log", lambda date: None)
    monkeypatch.setattr(db, "get_config", lambda: dict(saved))
    stale = resolve_day_config(None, 0.0)
    eff, src = _effective_today(_Svc("idle", None, stale))

    assert src == "not_armed"
    assert eff["instrument"] == "atm_option"
    assert eff["margin_per_slot"] == 60000.0
    assert eff["grade_sides"] == "A:LS,B:-,C:-"
    assert eff == resolve_day_config(saved, 0.0)  # exactly what arm() resolves


def test_a_failing_preview_loader_degrades_to_the_snapshot(monkeypatch):
    """The endpoint backs the config FORM: a raising loader must not 500 it."""
    import database.open15_breakout_db as db
    import services.open15_breakout_service as mod
    from blueprints.open15_breakout import _effective_today

    def boom():
        raise RuntimeError("loader down")

    monkeypatch.setattr(db, "get_day_log", lambda date: None)
    monkeypatch.setattr(mod, "load_arm_config", boom)
    eff, src = _effective_today(_Svc("idle"))

    assert src == "not_armed" and eff == {"instrument": "stock", "max_trades": 3}


def test_arm_and_preview_share_one_loader():
    """One function, so the preview and the arm cannot drift (#764)."""
    import inspect

    import services.open15_breakout_service as mod

    src = inspect.getsource(mod.Open15BreakoutService.arm)
    assert "load_arm_inputs()" in src
    assert "get_config()" not in src
    assert "load_arm_inputs()" in inspect.getsource(mod.load_arm_config)


def test_yesterdays_arm_is_not_served_as_todays(monkeypatch):
    """`day_status` alone is not enough — a process armed yesterday and left
    running overnight still holds that snapshot."""
    import database.open15_breakout_db as db
    from blueprints.open15_breakout import _effective_today

    monkeypatch.setattr(db, "get_day_log", lambda date: None)
    _eff, src = _effective_today(_Svc("armed", "2020-01-01"))

    assert src == "not_armed"


def test_an_unreadable_day_log_degrades_instead_of_500ing(monkeypatch):
    """This endpoint backs the config FORM. It failing takes the operator's
    only control surface with it, so a log read that raises must not."""
    import database.open15_breakout_db as db
    from blueprints.open15_breakout import _effective_today

    def boom(date):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_day_log", boom)
    eff, src = _effective_today(_Svc("idle"))

    assert src == "not_armed" and eff is not None


def test_the_last_arm_of_the_day_wins(monkeypatch):
    """A late-boot restart can arm twice; the most recent snapshot is the one
    the rest of the session ran under."""
    import database.open15_breakout_db as db
    from blueprints.open15_breakout import _effective_today

    first = {**ARMED_2026_08_19, "margin_effective": 30000.0}
    second = {**ARMED_2026_08_19, "margin_effective": 60000.0}
    monkeypatch.setattr(db, "get_day_log", lambda date: [first, {"event": "selection"}, second])
    eff, src = _effective_today(_Svc("idle"))

    assert src == "armed_log" and eff["margin_effective"] == 60000.0


# --------------------------------------------------------------------------- #
# 3. The page — the label reaches the line, and no "undefined" leverage
# --------------------------------------------------------------------------- #
def _render_eff(effective, source):
    """Run the page's OWN effective-line JS in node (the #643 lesson: only
    executing it catches a bad identifier or a stringified undefined)."""
    import json
    import shutil
    import subprocess

    from blueprints.open15_breakout import _LOGS_PAGE

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    fn = _LOGS_PAGE.split("function effMaxTrades(e){")[1].split("\nasync function loadCfg(")[0]
    fn = fn.rstrip().removesuffix("}")  # its own brace; the wrapper adds one
    body = _LOGS_PAGE.split("  const e=j.effective_today;\n")[1].split("\nasync function saveCfg(")[
        0
    ]
    body = body.rstrip().removesuffix("}")
    # the per-grade side helpers the line formats with (issue #762)
    gs = (
        "const GS_SIDES="
        + _LOGS_PAGE.split("const GS_SIDES=")[1].split("function gsFromForm(){")[0]
    )
    script = (
        "const out={};\n"
        "const document={getElementById:()=>({set textContent(v){out.t=v;}})};\n"
        f"const j={json.dumps({'effective_today': effective, 'effective_source': source})};\n"
        f"{gs}\n"
        f"function effMaxTrades(e){{{fn}}}\n"
        "const e=j.effective_today;\n"
        f"{body}\n"
        "console.log(JSON.stringify(out));"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["t"]


def test_the_line_names_the_arm_it_came_from_after_a_restart():
    line = _render_eff(effective_from_armed(ARMED_2026_08_19), "armed_log")

    assert line.startswith("effective today (from the 09:10 arm")
    assert "restarted since" in line
    # and it now agrees with the capital card two rows below it
    assert "atm_option" in line and "2 (of 3 configured" in line
    assert "margin 60000" in line
    assert "rolling watch-list every 30s" in line


def test_a_day_that_never_armed_is_not_presented_as_the_days_settings():
    line = _render_eff({"instrument": "stock", "max_trades": 3, "notional": 150000}, "not_armed")

    assert line.startswith("not armed today")
    assert "effective today" not in line


def test_a_missing_leverage_never_prints_undefined():
    """The armed event has no `leverage` key; `'x '+undefined` is a silent
    string, not an error, so only rendering catches it."""
    line = _render_eff(effective_from_armed(ARMED_2026_08_19), "armed_log")

    assert "undefined" not in line
    assert "notional 300000" in line


def test_the_live_armed_path_is_unchanged():
    """Byte-identical to pre-#645 for the case that always worked."""
    day_config = {
        "instrument": "atm_option",
        "trade_side": "both",
        "sizing_mode": "fixed",
        "margin_effective": 60000,
        "margin_per_slot": 60000,
        "leverage": 5,
        "notional": 300000,
        "max_trades": 2,
        "vol_mult": 1.5,
        "no_entry_after": "09:29",
        "exit_time": "09:30",
        "rolling_watchlist_enabled": False,
        "shadow_side": None,
    }
    line = _render_eff(day_config, "armed")

    assert line.startswith("effective today: atm_option")
    assert ") x 5 = notional 300000" in line
    assert "undefined" not in line


def test_the_line_states_the_per_grade_sides():
    """issue #762 - the policy is printed per grade, and a grade/side the trade
    side never watches is flagged on the same line; a pre-#762 day (no
    ``grade_sides``) renders the policy its ``trade_grades`` resolved to."""
    base = {
        "instrument": "stock",
        "trade_side": "long_only",
        "sizing_mode": "fixed",
        "margin_effective": 30000,
        "margin_per_slot": 30000,
        "leverage": 5,
        "notional": 150000,
        "max_trades": 3,
        "vol_mult": 1.5,
    }
    line = _render_eff(
        {**base, "grade_sides": "A:LS,B:L,C:-", "grade_sides_blocked": ["A short"]}, "armed"
    )
    # node stdout is decoded with the console code page on Windows, so match
    # around the non-ASCII separator rather than on it
    assert "grades A both" in line and "B long" in line and "C paper" in line
    assert "A short never watched" in line
    legacy = _render_eff({**base, "trade_grades": "AB"}, "armed")
    assert "grades A long" in legacy and "B long" in legacy and "C paper" in legacy
    assert "undefined" not in legacy
