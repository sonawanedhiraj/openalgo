"""Tests for the open15 option-liquidity score (issue #583).

Each test here pins a decision that was wrong in an earlier draft and was corrected
by measurement. They are regressions, not coverage: if one fails, the specific
mistake it names has come back.
"""

import datetime as dt

import pytest

from services import option_liquidity_service as ols


def _q(ltp, volume, oi, bid=None, ask=None):
    return {"ltp": ltp, "volume": volume, "oi": oi, "bid": bid, "ask": ask}


def _c(symbol, strike, lot=100, tick=0.05, expiry=dt.date(2026, 8, 25)):
    return {
        "symbol": symbol,
        "strike": strike,
        "expiry": expiry,
        "lotsize": lot,
        "ticksize": tick,
    }


# ---------------------------------------------------------------------------
# score_band
# ---------------------------------------------------------------------------


def test_premium_turnover_has_no_lot_multiply():
    """Broker ``volume`` is already in UNITS — multiplying by lot size again would
    inflate turnover by the lot factor and make big-lot names look 30x deeper.

    This is the #555 unit trap. The NSE bhavcopy reports LOTS and needs the multiply;
    the broker quote does not.
    """
    contracts = [_c("X26AUG100CE", 100, lot=1350)]
    quotes = {("X26AUG100CE", "NFO"): _q(ltp=30.0, volume=255150, oi=552150)}
    out = ols.score_band(contracts, quotes)
    assert out["atm_premium_turnover"] == pytest.approx(255150 * 30.0)
    # and the *count* is reported in lots, not units
    assert out["atm_volume_lots"] == pytest.approx(255150 / 1350, rel=1e-3)
    assert out["atm_oi_lots"] == pytest.approx(552150 / 1350, rel=1e-3)


def test_zero_volume_strikes_counted_and_spread_is_median_of_mid():
    contracts = [_c("A", 100), _c("B", 105), _c("C", 110)]
    quotes = {
        ("A", "NFO"): _q(10.0, 1000, 5000, bid=9.9, ask=10.1),  # 2% of mid
        ("B", "NFO"): _q(5.0, 0, 100, bid=4.5, ask=5.5),  # dead, 20% of mid
        ("C", "NFO"): _q(2.0, 500, 900, bid=1.99, ask=2.01),  # 1% of mid
    }
    out = ols.score_band(contracts, quotes)
    assert out["atm_zero_vol_strikes"] == 1
    assert out["band_strikes"] == 3
    assert out["atm_spread_pct"] == pytest.approx(2.0, abs=0.01)  # median of 1/2/20


def test_missing_quotes_are_not_counted_as_dead_strikes():
    """A contract the sweep never got back is *unmeasured*, not zero-volume.

    Conflating the two would punish a symbol for our own partial sweep.
    """
    contracts = [_c("A", 100), _c("B", 105)]
    out = ols.score_band(contracts, {("A", "NFO"): _q(10.0, 1000, 5000)})
    assert out["band_strikes"] == 1
    assert out["atm_zero_vol_strikes"] == 0


def test_broker_path_reports_no_trade_count():
    """``atm_trades``/``avg_ticket_inr`` are NULL on the broker path — the quote
    carries no trade count. NULL, never 0: "not captured" and "nothing traded" are
    different facts."""
    out = ols.score_band([_c("A", 100)], {("A", "NFO"): _q(10.0, 1000, 5000)})
    assert out["atm_trades"] is None
    assert out["avg_ticket_inr"] is None


# ---------------------------------------------------------------------------
# assign_percentiles
# ---------------------------------------------------------------------------


def _scored(**by_key):
    out = {}
    for key, (turnover, dead, band) in by_key.items():
        sym, side = key.split("_")
        out[(sym, side)] = {
            "symbol": sym,
            "side": side,
            "atm_premium_turnover": turnover,
            "atm_zero_vol_strikes": dead,
            "band_strikes": band,
        }
    return out


def test_sides_are_ranked_independently_never_blended():
    """The 14-of-208 misclassification regression.

    UNOMINDA-shaped: a usable call book and a thin put book (median CE p28 / PE p10).
    Its CE must rank clearly above its PE. If both come back equal the sides have been
    blended, and the 17-of-208 names that are thin on only one side get silently
    rescued (or condemned) by the other.

    Synthetic fixtures on purpose: a real symbol's ranking moves, and a test pinned to
    live data would rot. FORTIS was cited here originally and turned out to be a
    single-day outlier - see the research doc's 4a correction.
    """
    peers = {}
    for i in range(1, 5):
        # peers sit BETWEEN FORTIS's two sides, so FORTIS ranks near the top on
        # calls and near the bottom on puts. A blended score would land it mid-pack
        # on both — which is exactly the 14-name misclassification.
        peers[f"P{i}_CE"] = (10_00_00_000.0 * i, 0, 6)
        peers[f"P{i}_PE"] = (10_00_00_000.0 * i, 0, 6)
    scored = _scored(
        UNOMINDA_CE=(42_71_00_000.0, 0, 6),  # usable calls — above every peer but P4
        UNOMINDA_PE=(3_94_00_000.0, 0, 6),  # thin puts    — below every peer
        **peers,
    )
    ols.assign_percentiles(scored)
    ce = scored[("UNOMINDA", "CE")]["daily_pctile"]
    pe = scored[("UNOMINDA", "PE")]["daily_pctile"]
    assert ce > 60, f"UNOMINDA calls should rank high, got p{ce}"
    assert pe < 20, f"UNOMINDA puts should rank low, got p{pe}"
    assert ce - pe > 40, "the two sides must not collapse toward each other"


def test_turnover_drives_the_rank_not_ticket_count():
    """The MANAPPURAM regression.

    A block-traded name — few, large tickets — must rank on the rupees that changed
    hands. Nothing in the scorer may consult a trade count, so a name with high
    turnover outranks a name with low turnover regardless of any other field.
    """
    scored = _scored(
        BLOCKY_CE=(9_65_00_000.0, 0, 6),  # MANAPPURAM shape: big turnover
        RETAIL_CE=(2_49_00_000.0, 0, 6),  # GODFRYPHLP shape: smaller turnover
        TINY_CE=(45_00_000.0, 0, 6),
    )
    ols.assign_percentiles(scored)
    assert scored[("BLOCKY", "CE")]["daily_pctile"] > scored[("RETAIL", "CE")]["daily_pctile"]
    assert scored[("RETAIL", "CE")]["daily_pctile"] > scored[("TINY", "CE")]["daily_pctile"]
    # ordering only — the bottom live book is NOT 0.0, which stays reserved for the
    # dead-band floor
    assert scored[("TINY", "CE")]["daily_pctile"] > 0.0


def test_dead_band_forces_the_floor_regardless_of_turnover():
    """The BAJAJHLDNG hard tell: half the band trading nothing disqualifies the
    symbol even if the surviving strikes carried respectable turnover."""
    scored = _scored(
        DEAD_CE=(50_00_00_000.0, 3, 6),  # highest turnover in the set
        OK_CE=(1_00_00_000.0, 0, 6),
        OK2_CE=(2_00_00_000.0, 0, 6),
    )
    ols.assign_percentiles(scored)
    assert scored[("DEAD", "CE")]["daily_pctile"] == 0.0
    # an exact 0.0 is reserved for the forced floor: even the WORST live book scores
    # above it, so the two causes stay distinguishable downstream
    assert scored[("OK", "CE")]["daily_pctile"] > 0.0
    assert scored[("OK2", "CE")]["daily_pctile"] > scored[("OK", "CE")]["daily_pctile"]


def test_unmeasured_symbol_gets_no_percentile_rather_than_zero():
    scored = _scored(GOOD_CE=(1_00_00_000.0, 0, 6))
    scored[("GHOST", "CE")] = {
        "symbol": "GHOST",
        "side": "CE",
        "atm_premium_turnover": None,
        "atm_zero_vol_strikes": None,
        "band_strikes": 0,
    }
    ols.assign_percentiles(scored)
    assert scored[("GHOST", "CE")].get("daily_pctile") is None


# ---------------------------------------------------------------------------
# apply_median
# ---------------------------------------------------------------------------


def test_insufficient_history_is_null_not_a_low_score():
    """A newly listed F&O name must report "cannot rank" rather than "illiquid".

    Scoring it low would be indistinguishable from scoring it on no evidence, and a
    consumer would exclude it for the wrong reason.
    """
    scored = {("NEW", "CE"): {"daily_pctile": 5.0}}
    ols.apply_median(scored, {("NEW", "CE"): [4.0, 6.0]}, min_days=10, median_days=20)
    assert scored[("NEW", "CE")]["option_liquidity_pctile"] is None
    assert scored[("NEW", "CE")]["n_days_in_median"] == 3


def test_median_smooths_a_single_day_spike():
    """The V3 stability result in miniature: one wild day must not move the score.

    Single-day scoring churned ~30 names a day (Jaccard 0.48); the median cut that to
    3.4 (0.91). A build that reported today's percentile would fail this.
    """
    history = [20.0] * 19
    scored = {("X", "CE"): {"daily_pctile": 95.0}}  # LICI-shaped one-day spike
    ols.apply_median(scored, {("X", "CE"): history}, min_days=10, median_days=20)
    assert scored[("X", "CE")]["option_liquidity_pctile"] == pytest.approx(20.0)
    assert scored[("X", "CE")]["n_days_in_median"] == 20


def test_median_window_is_bounded():
    scored = {("X", "CE"): {"daily_pctile": 50.0}}
    ols.apply_median(scored, {("X", "CE"): [10.0] * 100}, min_days=10, median_days=20)
    assert scored[("X", "CE")]["n_days_in_median"] == 20


# ---------------------------------------------------------------------------
# resolve_band — the expiry trap
# ---------------------------------------------------------------------------


@pytest.fixture
def _contracts(monkeypatch):
    """Seed symtoken with one underlying across THREE contract months."""
    from database.symbol import SymToken, db_session

    db_session.query(SymToken).delete()
    rid = 0
    for exp in ("25-AUG-26", "29-SEP-26", "27-OCT-26"):
        for strike in (90, 95, 100, 105, 110):
            for itype in ("CE", "PE"):
                rid += 1
                db_session.add(
                    SymToken(
                        id=rid,
                        symbol=f"ZZ{exp[:2]}{exp[3:6]}26{strike}{itype}",
                        brsymbol=f"br{rid}",
                        name="ZZ",
                        exchange="NFO",
                        brexchange="NFO",
                        token=str(rid),
                        expiry=exp,
                        strike=float(strike),
                        lotsize=100,
                        instrumenttype=itype,
                        tick_size=0.05,
                    )
                )
    db_session.commit()
    yield
    db_session.query(SymToken).delete()
    db_session.commit()
    db_session.remove()


def test_band_takes_the_front_month_only(_contracts):
    """The same strike exists in all three contract months.

    A resolver that ignores expiry silently mixes months — the exact bug that made a
    validation pass report zero open interest for actively-traded contracts.
    """
    band = ols.resolve_band("ZZ", spot=100.0, trade_date=dt.date(2026, 8, 10), per_side=3)
    assert band["CE"] and band["PE"]
    assert {c["expiry"] for c in band["CE"] + band["PE"]} == {dt.date(2026, 8, 25)}


def test_band_is_nearest_strikes_per_side_and_sides_are_independent(_contracts):
    band = ols.resolve_band("ZZ", spot=100.0, trade_date=dt.date(2026, 8, 10), per_side=3)
    assert sorted(c["strike"] for c in band["CE"]) == [95.0, 100.0, 105.0]
    assert sorted(c["strike"] for c in band["PE"]) == [95.0, 100.0, 105.0]
    assert len(band["CE"]) == 3 and len(band["PE"]) == 3


def test_band_skips_expired_months(_contracts):
    """After the August expiry the front month must roll to September, never to an
    expired contract."""
    band = ols.resolve_band("ZZ", spot=100.0, trade_date=dt.date(2026, 8, 26), per_side=2)
    assert {c["expiry"] for c in band["CE"]} == {dt.date(2026, 9, 29)}


def test_band_rolls_when_consumed_on_a_blocked_day(_contracts):
    """The sweep's scores are consumed the NEXT morning (issue #669).

    Friday 2026-08-21's ~15:40 sweep feeds Monday 2026-08-24 — inside the
    broker's physical-delivery block window for the 25-AUG expiry — so the band
    must price September, or the #591 ladder quotes lot costs on contracts the
    strategy cannot buy that day. Same for Monday's sweep (consumed on expiry
    day), and for the expiry-day sweep itself (consumed Wednesday, when the
    front month is DEAD, not merely blocked).
    """
    for sweep_day in (dt.date(2026, 8, 21), dt.date(2026, 8, 24), dt.date(2026, 8, 25)):
        band = ols.resolve_band("ZZ", spot=100.0, trade_date=sweep_day, per_side=2)
        assert {c["expiry"] for c in band["CE"] + band["PE"]} == {dt.date(2026, 9, 29)}, sweep_day


def test_band_keeps_front_month_outside_the_window(_contracts):
    """Mid-cycle (Wed 2026-08-19 sweep -> consumed Thu 2026-08-20) the front
    month stays — the roll is two days a month, never a general preference for
    the back month."""
    band = ols.resolve_band("ZZ", spot=100.0, trade_date=dt.date(2026, 8, 19), per_side=2)
    assert {c["expiry"] for c in band["CE"]} == {dt.date(2026, 8, 25)}


def test_band_empty_without_a_spot(_contracts):
    assert ols.resolve_band("ZZ", spot=0.0, trade_date=dt.date(2026, 8, 10)) == {
        "CE": [],
        "PE": [],
    }


# ---------------------------------------------------------------------------
# universe reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_reports_both_directions(monkeypatch, _contracts):
    monkeypatch.setenv("SCANNER_SYMBOLS", "ZZ,GONE,NIFTY")
    monkeypatch.setattr(
        ols, "load_equity_universe", lambda: {"ZZ", "GONE"}
    )  # indices already dropped
    r = ols.reconcile_universe()
    assert r["missing_contracts"] == ["GONE"]
    assert "ZZ" not in r["missing_contracts"]


# ---------------------------------------------------------------------------
# job wiring
# ---------------------------------------------------------------------------


def test_non_trading_day_writes_nothing(monkeypatch):
    monkeypatch.setattr(
        "services.data_freshness_service.is_trading_day", lambda d, exchange=None: False
    )
    out = ols.run_for_date(dt.date(2026, 8, 9))  # a Sunday
    assert out["status"] == "skipped_non_trading_day"
    assert out["rows"] == 0


def test_no_broker_session_writes_nothing(monkeypatch):
    """A missing session must produce NO row rather than a partial or stale one.

    The score is a 20-day median, so a skipped day barely moves it; a half-swept day
    would corrupt the percentile, which is a rank within that day's universe.
    """
    monkeypatch.setattr(
        "services.data_freshness_service.is_trading_day", lambda d, exchange=None: True
    )
    monkeypatch.setattr("database.auth_db.get_first_available_api_key", lambda: None)
    out = ols.run_for_date(dt.date(2026, 8, 7))
    assert out["status"] == "skipped_no_session"
    assert out["rows"] == 0


# ---------------------------------------------------------------------------
# sweep credibility — the all-zero feed guard
# ---------------------------------------------------------------------------


def test_all_zero_sweep_is_rejected():
    """A closed market (or a dead feed) returns LTP but zeroes volume/OI/bid/ask.

    Observed live on a Sunday: all 416 underlying-sides scored zero turnover with 6/6
    dead strikes. Scored naively that reads as "the entire F&O universe is illiquid".
    It must be discarded, not persisted — a bad row would sit in the 20-day median for
    four weeks.
    """
    rows = [{"symbol": f"S{i}", "side": "CE", "atm_premium_turnover": 0.0} for i in range(50)]
    ok, stats = ols.sweep_is_credible(rows)
    assert not ok
    assert stats["dead_frac"] == 1.0


def test_a_few_dead_names_are_normal():
    """Genuinely thin names exist — BAJAJHLDNG scored zero on real days. Only a
    MAJORITY of the universe being dead indicts the sweep rather than the market."""
    rows = [{"symbol": f"S{i}", "side": "CE", "atm_premium_turnover": 1000.0} for i in range(48)]
    rows += [{"symbol": "DEAD1", "side": "CE", "atm_premium_turnover": 0.0}]
    rows += [{"symbol": "DEAD2", "side": "CE", "atm_premium_turnover": None}]
    ok, stats = ols.sweep_is_credible(rows)
    assert ok
    assert stats["dead"] == 2


def test_empty_sweep_is_not_credible():
    ok, _ = ols.sweep_is_credible([])
    assert not ok


def test_incredible_sweep_writes_nothing(monkeypatch):
    """End-to-end: the guard must stop the WRITE, not merely log."""
    monkeypatch.setattr(
        "services.data_freshness_service.is_trading_day", lambda d, exchange=None: True
    )
    monkeypatch.setattr("database.auth_db.get_first_available_api_key", lambda: "k")
    monkeypatch.setattr(
        ols,
        "compute_scores",
        lambda *a, **k: [
            {"symbol": f"S{i}", "side": "CE", "atm_premium_turnover": 0.0} for i in range(30)
        ],
    )
    monkeypatch.setattr(
        ols, "reconcile_universe", lambda: {"missing_contracts": [], "unwatched_with_options": []}
    )

    def _boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("upsert_scores must not be called for an incredible sweep")

    monkeypatch.setattr("database.option_liquidity_db.upsert_scores", _boom)
    out = ols.run_for_date(dt.date(2026, 8, 7))
    assert out["status"] == "discarded_not_credible"
    assert out["written"] == 0


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


@pytest.fixture
def _db():
    from database import option_liquidity_db as db

    db.init_db()
    db.db_session.query(db.OptionLiquidityDaily).delete()
    db.db_session.commit()
    db.db_session.remove()
    yield db
    db.db_session.query(db.OptionLiquidityDaily).delete()
    db.db_session.commit()
    db.db_session.remove()


_UNSET = object()


def _row(sym, side, pctile, daily=_UNSET, turnover=1.0):
    # `daily=None` must mean a genuinely unmeasured day, so the default is a
    # sentinel rather than None — otherwise the helper silently backfills the
    # very case the history test is trying to exercise
    return {
        "symbol": sym,
        "side": side,
        "atm_premium_turnover": turnover,
        "atm_zero_vol_strikes": 0,
        "band_strikes": 6,
        "option_liquidity_pctile": pctile,
        "daily_pctile": pctile if daily is _UNSET else daily,
        "n_days_in_median": 20,
        "expiry_used": dt.date(2026, 8, 25),
    }


def test_upsert_is_idempotent_per_date(_db):
    d = dt.date(2026, 8, 7)
    assert _db.upsert_scores(d, [_row("A", "CE", 10.0), _row("A", "PE", 40.0)]) == 2
    # re-running the day REPLACES rather than accumulating
    assert _db.upsert_scores(d, [_row("A", "CE", 11.0)]) == 1
    rows = _db.get_scores_for_date(d)
    assert len(rows) == 1
    assert rows[0]["option_liquidity_pctile"] == 11.0


def test_stale_scores_return_empty_so_callers_fail_open(_db):
    """A score older than ``max_age_days`` must read as "no data", never as a verdict.

    Consumers fail OPEN on an empty result. Returning a stale row instead would let a
    collection outage quietly dark the universe — the #390 failure shape, where 3
    stale symbols of 216 held the whole scanner all session.
    """
    _db.upsert_scores(dt.date(2026, 8, 1), [_row("A", "CE", 10.0)])
    fresh = _db.get_latest_scores(max_age_days=3, today=dt.date(2026, 8, 3))
    assert fresh and ("A", "CE") in fresh
    stale = _db.get_latest_scores(max_age_days=3, today=dt.date(2026, 8, 20))
    assert stale == {}


def test_no_scores_at_all_returns_empty(_db):
    assert _db.get_latest_scores(max_age_days=3, today=dt.date(2026, 8, 20)) == {}


def test_history_excludes_today_and_skips_unmeasured_days(_db):
    """The median input must not include the day being scored (the caller prepends
    it), and a day we failed to measure is skipped rather than counted as zero."""
    _db.upsert_scores(dt.date(2026, 8, 3), [_row("A", "CE", 30.0, daily=30.0)])
    _db.upsert_scores(dt.date(2026, 8, 4), [_row("A", "CE", 40.0, daily=None)])
    _db.upsert_scores(dt.date(2026, 8, 5), [_row("A", "CE", 50.0, daily=50.0)])
    hist = _db.get_daily_pctile_history(10, dt.date(2026, 8, 5))
    assert hist[("A", "CE")] == [30.0]  # 08-05 excluded (it IS today), 08-04 has no daily


def test_registered_job_id_is_catalogued():
    """``test_scheduler_registry`` enforces this globally; asserting it here too means
    a failure names the job that drifted."""
    from services.scheduler_registry import CATALOG

    assert any(j.job_id == "option_liquidity_eod" for j in CATALOG)


# ---------------------------------------------------------------------------
# Trading-day-aware staleness (issue #589)
# ---------------------------------------------------------------------------


def test_sessions_behind_weekend_counts_zero(_db):
    """Friday's scores are 0 sessions behind all weekend, 1 on Monday, 2 on
    Tuesday. (The temp DB has no 2026 holiday rows, so ``is_trading_day``
    fail-opens to weekday-only — deterministic here.)"""
    fri = dt.date(2026, 8, 7)
    assert _db.sessions_behind(fri, dt.date(2026, 8, 8)) == 0  # Sat
    assert _db.sessions_behind(fri, dt.date(2026, 8, 9)) == 0  # Sun
    assert _db.sessions_behind(fri, dt.date(2026, 8, 10)) == 1  # Mon
    assert _db.sessions_behind(fri, dt.date(2026, 8, 11)) == 2  # Tue


def test_the_2026_08_11_outage_shape_stays_fresh(_db):
    """The exact regression that filed #589: Friday scores + weekend + one-day
    outage = 4 CALENDAR days read as stale, with the data only 2 SESSIONS old.
    The gate must keep its data here."""
    _db.upsert_scores(dt.date(2026, 8, 7), [_row("A", "CE", 10.0)])
    fresh = _db.get_latest_scores(max_age_days=3, today=dt.date(2026, 8, 11))
    assert fresh and ("A", "CE") in fresh


def test_holiday_monday_does_not_burn_the_staleness_budget(_db, monkeypatch):
    """Fri 2026-09-11 scores at Tue 09-15's arm, with Ganesh Chaturthi Monday
    (09-14) in between: 1 session behind -> fresh. The pre-#589 calendar check
    read this healthy long weekend as 4 days = stale."""
    holiday = dt.date(2026, 9, 14)
    monkeypatch.setattr(
        "services.data_freshness_service.is_trading_day",
        lambda d, exchange=None: d.weekday() < 5 and d != holiday,
    )
    assert _db.sessions_behind(dt.date(2026, 9, 11), dt.date(2026, 9, 15)) == 1
    _db.upsert_scores(dt.date(2026, 9, 11), [_row("A", "CE", 10.0)])
    fresh = _db.get_latest_scores(max_age_days=3, today=dt.date(2026, 9, 15))
    assert fresh and ("A", "CE") in fresh


def test_genuinely_stale_scores_still_fail_open(_db):
    """More than max_age_days SESSIONS behind -> empty dict, callers fail open.
    Fri 08-07 .. Fri 08-14 = 5 sessions."""
    _db.upsert_scores(dt.date(2026, 8, 7), [_row("A", "CE", 10.0)])
    assert _db.get_latest_scores(max_age_days=3, today=dt.date(2026, 8, 14)) == {}


def test_sessions_behind_walk_is_capped(_db):
    """A gap past the cap horizon reports behind regardless — no unbounded walk."""
    assert _db.sessions_behind(dt.date(2026, 1, 1), dt.date(2026, 8, 1)) > 3


# ---------------------------------------------------------------------------
# Missed-sweep convergence loop (issue #589)
# ---------------------------------------------------------------------------


def _ist(y, mo, d, h, mi):
    import pytz

    return pytz.timezone("Asia/Kolkata").localize(dt.datetime(y, mo, d, h, mi))


@pytest.fixture()
def _conv(monkeypatch):
    """Common convergence-tick harness: trading day, records run_for_date calls."""
    calls = []
    monkeypatch.setattr(
        "services.data_freshness_service.is_trading_day", lambda d, exchange=None: True
    )
    monkeypatch.setattr(ols, "run_for_date", lambda *a, **k: calls.append(a) or {"status": "ok"})
    monkeypatch.setattr("database.option_liquidity_db.has_scores_for", lambda d: False)
    return calls


def test_convergence_runs_when_day_unscored_after_sweep_time(_conv):
    # Tue 2026-08-11 16:30 IST, trading day, no rows -> catch-up sweep
    assert ols._convergence_tick(_ist(2026, 8, 11, 16, 30)) is True
    assert len(_conv) == 1


def test_convergence_waits_for_the_cron_grace(_conv):
    # 15:50 is inside the 15:45+10min grace — the cron fire owns the slot
    assert ols._convergence_tick(_ist(2026, 8, 11, 15, 50)) is False
    assert _conv == []


def test_convergence_noops_when_day_already_scored(_conv, monkeypatch):
    monkeypatch.setattr("database.option_liquidity_db.has_scores_for", lambda d: True)
    assert ols._convergence_tick(_ist(2026, 8, 11, 16, 30)) is False
    assert _conv == []


def test_convergence_noops_on_non_trading_day(_conv, monkeypatch):
    monkeypatch.setattr(
        "services.data_freshness_service.is_trading_day", lambda d, exchange=None: False
    )
    assert ols._convergence_tick(_ist(2026, 8, 9, 16, 30)) is False
    assert _conv == []


def test_convergence_respects_its_flag(_conv, monkeypatch):
    monkeypatch.setenv("OPTION_LIQUIDITY_CONVERGENCE_ENABLED", "false")
    assert ols._convergence_tick(_ist(2026, 8, 11, 16, 30)) is False
    assert _conv == []


def test_convergence_thread_is_catalogued():
    """``test_thread_registry`` enforces this globally; asserting here names the
    thread if it drifts."""
    from services.thread_registry import CATALOG

    assert any(t.thread_name == "OptionLiquidityConvergence" for t in CATALOG)
