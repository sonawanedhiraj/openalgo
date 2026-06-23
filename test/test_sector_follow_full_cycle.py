"""Integration tests for the sector_follow CAP5_VOL 15:20 entry full cycle.

P0-T4 from docs/research/observability/2026-06-22_production_flows_test_plan.md
(Flow 5). Covers the end-to-end path:

    scanner aggregator → _compute_metrics → passes_gates → select_entries →
    order_placer → sector_follow_trades journal

These tests wire the REAL in-memory databases (sector_follow_db,
strategy_runtime_override_db) so the journal-write and override-DB paths are
exercised for real — not just collected in a lambda list.  Order placement is
mocked at the ``production_order_placer`` boundary (a lambda returning a
success dict) so no sandbox.db or live broker is required.

Five scenario groups (all P0):
  1. Signal evaluation — >5 qualifying symbols → ≤5 selected, ranked by
     vol_ratio descending; all data sourced from the mock scanner aggregator.
  2. Sandbox journal write — sector_follow_trades rows are written to a real
     (temp) SQLite DB with correct symbol / side / mode / status.
  3. Order routing — sandbox dispatches to the order placer with the right
     payload; scaffold short-circuits without any dispatch.
  4. Stale feed — data_freshness_service reports stale → run_entry() returns []
     + Telegram alert; no journal rows written; run_exit() still runs.
  5. Smoke check abort — assert_data_pipeline_healthy() at 15:18 IST writes a
     strategy_runtime_override pause row → 15:20 run_entry() is blocked
     (_entry_held_by_override returns True).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from services.sector_follow_service import (
    SectorFollowConfig,
    SectorFollowService,
    select_entries,
)

_IST = timezone(timedelta(hours=5, minutes=30))

# Representative universe (7 of the LOCK_STATIC_30 set) with sector mappings.
_UNIVERSE = ["HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "RELIANCE", "TATAPOWER", "NTPC"]
_SECTOR_MAP = {
    "HDFCBANK": "BANKNIFTY",
    "ICICIBANK": "BANKNIFTY",
    "KOTAKBANK": "BANKNIFTY",
    "AXISBANK": "BANKNIFTY",
    "RELIANCE": "NIFTY",
    "TATAPOWER": "NIFTY",
    "NTPC": "NIFTY",
}
_SECTOR_INDICES = ["BANKNIFTY", "NIFTY"]


# --------------------------------------------------------------------------- #
# DB isolation fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate_sro_db(monkeypatch):
    """Rebind strategy_runtime_override_db to a fresh in-memory SQLite per test.

    autouse so override writes from kill-switch / smoke-check / auto-pause never
    leak between tests (mirrors the pattern in test_sector_follow_service.py).
    """
    from database import strategy_runtime_override_db as sro

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(sro, "engine", eng)
    monkeypatch.setattr(sro, "db_session", sess)
    sro.Base.query = sess.query_property()
    sro.Base.metadata.create_all(eng)
    yield sro
    sess.remove()
    eng.dispose()


@pytest.fixture
def _isolate_journal_db(monkeypatch):
    """Rebind sector_follow_db to a fresh in-memory SQLite per test.

    Opt-in (not autouse): only tests that assert actual sector_follow_trades
    rows use this fixture.
    """
    from database import sector_follow_db as sfdb

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(sfdb, "engine", eng)
    monkeypatch.setattr(sfdb, "db_session", sess)
    sfdb.Base.query = sess.query_property()
    sfdb.Base.metadata.create_all(eng)
    yield sfdb
    sess.remove()
    eng.dispose()


# --------------------------------------------------------------------------- #
# Test helpers
# --------------------------------------------------------------------------- #


def _config(**overrides) -> SectorFollowConfig:
    base = {
        "capital_inr": 250_000.0,
        "max_position_inr": 50_000.0,
        "max_concurrent_positions": 5,
        "gate_sector_pct": 1.0,
        "gate_stock_pct": 0.5,
        "gate_vol_mult": 1.0,
        "daily_loss_kill_pct": 3.0,
        "cost_pct_round_trip": 0.0857,
        "vol_avg_lookback_days": 20,
        "broker": "zerodha",
        "exchange": "NSE",
        "product": "CNC",
        "universe": list(_UNIVERSE),
        "strategy_id": 99,
    }
    base.update(overrides)
    return SectorFollowConfig(**base)


def _ist_epoch(y, mo, d, h, mi) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=_IST).timestamp()


def _prior_history(close: float = 100.0, avg_vol: float = 1_000.0, n_days: int = 2) -> list:
    """Build ``n_days`` prior-day 1m bars (each at 15:29 IST on consecutive days
    before 2026-06-23) giving ``prior_close=close`` and uniform ``avg_vol``."""
    rows = []
    base = datetime(2026, 6, 21, 15, 29, tzinfo=_IST)  # Saturday — safely prior to Mon 23
    for i in range(n_days - 1, -1, -1):
        d = base - timedelta(days=i)
        rows.append((_ist_epoch(d.year, d.month, d.day, 15, 29), close, avg_vol))
    return rows


def _agg_provider(symbols_data: dict) -> callable:
    """Intraday provider backed by a static ``{symbol: (close, vol)}`` map."""

    def _provider(symbol, as_of):
        return symbols_data.get(symbol, (None, None))

    return _provider


def _hist_reader(history_map: dict) -> callable:
    """History reader backed by a static ``{symbol: [(ts, close, vol)...]}`` map."""

    def _reader(all_syms, window_start):
        return {s: list(history_map.get(s, [])) for s in all_syms}

    return _reader


def _make_service(
    aggregator_data: dict,
    history_data: dict,
    mode: str = "sandbox",
    trade_recorder=None,
    notifier=None,
    data_health_checker=None,
    broker_session_checker=None,
    now_dt: datetime | None = None,
    intent_source: str = "strategy_mode",
) -> tuple[SectorFollowService, list, list]:
    """Build a SectorFollowService wired to injected data sources.

    Returns ``(service, placed_orders, alerts)`` where the last two are lists
    that accumulate side effects during the test.
    """
    placed_orders: list = []
    alerts: list = []
    notifier = notifier or (lambda msg: alerts.append(msg))

    def fake_placer(mode_arg, order):
        placed_orders.append((mode_arg, order))
        return {"status": "success", "orderid": f"OID-{order['symbol']}"}

    from services.mode_service import EffectiveDecision

    cfg = _config()
    now_dt = now_dt or datetime(2026, 6, 23, 15, 20, tzinfo=_IST)

    svc = SectorFollowService(
        config=cfg,
        sector_map=dict(_SECTOR_MAP),
        mode=mode,
        intraday_provider=_agg_provider(aggregator_data),
        history_reader=_hist_reader(history_data),
        broker_session_checker=broker_session_checker or (lambda: True),
        order_placer=fake_placer,
        price_fetcher=lambda s, e: None,
        notifier=notifier,
        trade_recorder=trade_recorder,  # None → real _default_trade_recorder
        now=lambda: now_dt,
        intent_resolver=lambda: EffectiveDecision(
            mode=mode, intent="run", daily_capital_cap=None, source=intent_source
        ),
        data_health_checker=data_health_checker,
    )
    return svc, placed_orders, alerts


def _full_agg(extra: dict | None = None, today_close: float = 102.0) -> dict:
    """Aggregator map: all 7 universe stocks + 2 sector indices at ``today_close``."""
    agg = dict.fromkeys(_UNIVERSE, (today_close, 2_000.0))
    for idx in _SECTOR_INDICES:
        agg[idx] = (today_close, 0.0)
    if extra:
        agg.update(extra)
    return agg


def _full_hist(close: float = 100.0, avg_vol: float = 1_000.0) -> dict:
    """History map: 2 prior days for all universe symbols + sector indices."""
    return {
        s: _prior_history(close=close, avg_vol=avg_vol)
        for s in list(_UNIVERSE) + list(_SECTOR_INDICES)
    }


# =========================================================================== #
# Scenario 1 — Signal evaluation: ≤5 BUY signals, cap-by-vol-ratio
# =========================================================================== #


class TestSignalEvaluationCapByVolRatio:
    """When >5 universe symbols pass all gates the service selects ≤5,
    ranked by vol_ratio descending.  All data is sourced from the injected
    scanner aggregator (intraday_source='aggregator')."""

    # Vol ratios produce a clear ordering across 7 stocks.
    # avg_vol = 1_000, so today_vol = vol_ratio × 1_000.
    _VOL_RATIOS = {
        "ICICIBANK": 3.0,  # rank 1
        "HDFCBANK": 2.5,  # rank 2
        "AXISBANK": 2.0,  # rank 3
        "KOTAKBANK": 1.8,  # rank 4
        "RELIANCE": 1.6,  # rank 5
        "TATAPOWER": 1.3,  # rank 6 — cut
        "NTPC": 1.1,  # rank 7 — cut
    }

    def _agg(self) -> dict:
        agg = {sym: (102.0, vr * 1_000.0) for sym, vr in self._VOL_RATIOS.items()}
        for idx in _SECTOR_INDICES:
            agg[idx] = (102.0, 0.0)
        return agg

    def test_selects_at_most_5_signals(self):
        svc, _, _ = _make_service(
            self._agg(),
            _full_hist(),
            mode="scaffold",
            trade_recorder=lambda **kw: 1,
        )
        candidates = svc.evaluate_candidates()
        entries = select_entries(candidates, set(), max_concurrent=5)
        assert len(entries) <= 5

    def test_selects_exactly_top_5_by_vol_ratio(self):
        svc, _, _ = _make_service(
            self._agg(),
            _full_hist(),
            mode="scaffold",
            trade_recorder=lambda **kw: 1,
        )
        candidates = svc.evaluate_candidates()
        entries = select_entries(candidates, set(), max_concurrent=5)
        assert {e["symbol"] for e in entries} == {
            "ICICIBANK",
            "HDFCBANK",
            "AXISBANK",
            "KOTAKBANK",
            "RELIANCE",
        }

    def test_vol_ratio_ordering_is_strictly_descending(self):
        svc, _, _ = _make_service(
            self._agg(),
            _full_hist(),
            mode="scaffold",
            trade_recorder=lambda **kw: 1,
        )
        candidates = svc.evaluate_candidates()
        entries = select_entries(candidates, set(), max_concurrent=5)
        vrs = [e["vol_ratio"] for e in entries]
        assert vrs == sorted(vrs, reverse=True)

    def test_low_vol_ratio_symbols_excluded(self):
        svc, _, _ = _make_service(
            self._agg(),
            _full_hist(),
            mode="scaffold",
            trade_recorder=lambda **kw: 1,
        )
        candidates = svc.evaluate_candidates()
        entries = select_entries(candidates, set(), max_concurrent=5)
        selected = {e["symbol"] for e in entries}
        assert "TATAPOWER" not in selected
        assert "NTPC" not in selected

    def test_all_metrics_sourced_from_aggregator(self):
        """intraday_source must be 'aggregator' for every universe symbol when
        the mock scanner returns data for all of them."""
        svc, _, _ = _make_service(
            self._agg(),
            _full_hist(),
            mode="scaffold",
            trade_recorder=lambda **kw: 1,
        )
        metrics = svc._metrics_provider(svc._now(), svc.config.universe, svc.sector_map, svc.config)
        sources = {sym: m["intraday_source"] for sym, m in metrics.items()}
        non_agg = {sym for sym, src in sources.items() if src != "aggregator"}
        assert not non_agg, f"Expected all aggregator, non-aggregator symbols: {non_agg}"

    def test_sector_gate_blocks_symbol_when_index_below_threshold(self):
        """sector_ret ≤ 1.0% → symbol rejected even if stock and vol gates pass."""
        agg = dict(self._agg())
        # BANKNIFTY only up 0.9% today (prior_close=100, today=100.9 < +1%)
        agg["BANKNIFTY"] = (100.9, 0.0)
        svc, _, _ = _make_service(
            agg,
            _full_hist(),
            mode="scaffold",
            trade_recorder=lambda **kw: 1,
        )
        candidates = svc.evaluate_candidates()
        banking_syms = {"HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK"}
        selected = {c["symbol"] for c in candidates}
        assert not (selected & banking_syms), (
            f"Banking stocks should be rejected when BANKNIFTY < +1%, got: {selected & banking_syms}"
        )


# =========================================================================== #
# Scenario 2 — Sandbox mode: sector_follow_trades journal rows written
# =========================================================================== #


class TestSandboxJournalWrite:
    """Full cycle in sandbox mode: signals → place_entry →
    sector_follow_trades row written with correct symbol / side / mode / status.

    Uses a real (temp SQLite) journal DB via the _isolate_journal_db fixture."""

    def test_buy_rows_written_for_each_placed_entry(self, _isolate_journal_db):
        # Only 2 stocks with clear history to keep the assertion set small.
        agg_2 = {
            "HDFCBANK": (102.0, 2_000.0),
            "ICICIBANK": (102.0, 1_800.0),
            "BANKNIFTY": (102.0, 0.0),
            "NIFTY": (102.0, 0.0),
        }
        hist_2 = {s: _prior_history(close=100.0, avg_vol=1_000.0) for s in agg_2}
        svc, placed, _ = _make_service(agg_2, hist_2, mode="sandbox")
        svc.config.universe = ["HDFCBANK", "ICICIBANK"]
        svc.sector_map = {"HDFCBANK": "BANKNIFTY", "ICICIBANK": "BANKNIFTY"}

        placed_entries = svc.run_entry()
        assert len(placed_entries) == 2

        rows = _isolate_journal_db.SectorFollowTrade.query.all()
        assert len(rows) == 2
        for row in rows:
            assert row.side == "BUY"
            assert row.mode == "sandbox"
            assert row.status == "placed"
            assert row.symbol in {"HDFCBANK", "ICICIBANK"}
            assert row.quantity > 0
            assert row.order_id is not None

    def test_journal_row_has_cnc_product_and_nse_exchange(self, _isolate_journal_db):
        agg = {"RELIANCE": (102.0, 2_000.0), "NIFTY": (102.0, 0.0), "BANKNIFTY": (102.0, 0.0)}
        hist = {s: _prior_history(close=100.0, avg_vol=1_000.0) for s in agg}
        svc, _, _ = _make_service(agg, hist, mode="sandbox")
        svc.config.universe = ["RELIANCE"]
        svc.sector_map = {"RELIANCE": "NIFTY"}

        svc.run_entry()
        rows = _isolate_journal_db.SectorFollowTrade.query.all()
        assert len(rows) == 1
        assert rows[0].product == "CNC"
        assert rows[0].exchange == "NSE"

    def test_journal_row_price_matches_aggregator_close(self, _isolate_journal_db):
        agg = {"NTPC": (105.5, 2_000.0), "NIFTY": (105.5, 0.0), "BANKNIFTY": (105.5, 0.0)}
        hist = {s: _prior_history(close=100.0, avg_vol=1_000.0) for s in agg}
        svc, _, _ = _make_service(agg, hist, mode="sandbox")
        svc.config.universe = ["NTPC"]
        svc.sector_map = {"NTPC": "NIFTY"}

        svc.run_entry()
        rows = _isolate_journal_db.SectorFollowTrade.query.all()
        assert len(rows) == 1
        assert rows[0].price == pytest.approx(105.5)

    def test_journal_records_vol_ratio_and_returns(self, _isolate_journal_db):
        """vol_ratio, stock_ret, and sector_ret must be persisted so the operator
        can trace gate logic from the journal row."""
        agg = {"TATAPOWER": (102.0, 2_000.0), "NIFTY": (102.0, 0.0), "BANKNIFTY": (102.0, 0.0)}
        hist = {s: _prior_history(close=100.0, avg_vol=1_000.0) for s in agg}
        svc, _, _ = _make_service(agg, hist, mode="sandbox")
        svc.config.universe = ["TATAPOWER"]
        svc.sector_map = {"TATAPOWER": "NIFTY"}

        svc.run_entry()
        rows = _isolate_journal_db.SectorFollowTrade.query.all()
        assert len(rows) == 1
        row = rows[0]
        # vol = 2000, avg_vol = 1000 → vol_ratio ≈ 2.0
        assert row.vol_ratio == pytest.approx(2.0, rel=0.01)
        # stock_ret = 102/100 - 1 = 0.02
        assert row.stock_ret == pytest.approx(0.02, rel=0.01)
        # sector_ret = 102/100 - 1 = 0.02
        assert row.sector_ret == pytest.approx(0.02, rel=0.01)

    def test_failed_entry_journal_row_has_rejected_status(self, _isolate_journal_db):
        """A broker rejection must write a 'rejected' row and NOT add the symbol
        to paper_book (no phantom open position)."""
        agg = {"AXISBANK": (102.0, 2_000.0), "BANKNIFTY": (102.0, 0.0), "NIFTY": (102.0, 0.0)}
        hist = {s: _prior_history(close=100.0, avg_vol=1_000.0) for s in agg}
        svc, placed, _ = _make_service(agg, hist, mode="sandbox")
        svc.config.universe = ["AXISBANK"]
        svc.sector_map = {"AXISBANK": "BANKNIFTY"}
        # Override the placer to return an error
        svc._order_placer = lambda mode_arg, order: {
            "status": "error",
            "message": "insufficient margin",
        }

        result = svc.run_entry()
        assert result == []
        assert "AXISBANK" not in svc.paper_book

        rows = _isolate_journal_db.SectorFollowTrade.query.all()
        assert len(rows) == 1
        assert rows[0].status == "rejected"
        assert "insufficient margin" in (rows[0].error_message or "")


# =========================================================================== #
# Scenario 3 — Order routing: sandbox dispatches, scaffold does not
# =========================================================================== #


class TestOrderRouting:
    """Verify the dispatch fork: sandbox routes to the order placer;
    scaffold short-circuits without any dispatch."""

    def _simple_setup(self):
        agg = {"KOTAKBANK": (102.0, 2_000.0), "BANKNIFTY": (102.0, 0.0), "NIFTY": (102.0, 0.0)}
        hist = {s: _prior_history(close=100.0, avg_vol=1_000.0) for s in agg}
        return agg, hist

    def test_sandbox_calls_order_placer(self):
        agg, hist = self._simple_setup()
        svc, placed, _ = _make_service(agg, hist, mode="sandbox", trade_recorder=lambda **kw: 1)
        svc.config.universe = ["KOTAKBANK"]
        svc.sector_map = {"KOTAKBANK": "BANKNIFTY"}

        svc.run_entry()
        assert len(placed) == 1
        mode_arg, order = placed[0]
        assert mode_arg == "sandbox"
        assert order["symbol"] == "KOTAKBANK"
        assert order["action"] == "BUY"
        assert order["product"] == "CNC"

    def test_scaffold_does_not_call_order_placer(self):
        agg, hist = self._simple_setup()
        svc, placed, _ = _make_service(
            agg,
            hist,
            mode="scaffold",
            # source='env' prevents _apply_mode_override from mutating mode
            intent_source="env",
            trade_recorder=lambda **kw: 1,
        )
        svc.config.universe = ["KOTAKBANK"]
        svc.sector_map = {"KOTAKBANK": "BANKNIFTY"}

        svc.run_entry()
        assert placed == []

    def test_sandbox_order_does_not_include_a_limit_price(self):
        """MARKET order: the order dict must not carry a ``price`` field
        (the production order placer issues MARKET — no limit price)."""
        agg, hist = self._simple_setup()
        svc, placed, _ = _make_service(agg, hist, mode="sandbox", trade_recorder=lambda **kw: 1)
        svc.config.universe = ["KOTAKBANK"]
        svc.sector_map = {"KOTAKBANK": "BANKNIFTY"}

        svc.run_entry()
        assert len(placed) == 1
        _, order = placed[0]
        assert "price" not in order

    def test_sandbox_quantity_is_floor_of_max_position_over_price(self):
        """qty = floor(50_000 / 102.0) = 490 — verifies the sizing formula is
        applied to the aggregator close before dispatch."""
        agg, hist = self._simple_setup()
        svc, placed, _ = _make_service(agg, hist, mode="sandbox", trade_recorder=lambda **kw: 1)
        svc.config.universe = ["KOTAKBANK"]
        svc.sector_map = {"KOTAKBANK": "BANKNIFTY"}

        svc.run_entry()
        assert len(placed) == 1
        _, order = placed[0]
        import math

        expected_qty = math.floor(50_000.0 / 102.0)
        assert order["quantity"] == expected_qty

    def test_paper_book_populated_in_sandbox_mode(self):
        """After a successful sandbox entry the symbol appears in paper_book so
        the T+1 exit can find it."""
        agg, hist = self._simple_setup()
        svc, _, _ = _make_service(agg, hist, mode="sandbox", trade_recorder=lambda **kw: 1)
        svc.config.universe = ["KOTAKBANK"]
        svc.sector_map = {"KOTAKBANK": "BANKNIFTY"}

        svc.run_entry()
        assert "KOTAKBANK" in svc.paper_book

    def test_paper_book_populated_in_scaffold_mode_no_order(self):
        """Scaffold records the paper position WITHOUT placing an order."""
        agg, hist = self._simple_setup()
        svc, placed, _ = _make_service(
            agg,
            hist,
            mode="scaffold",
            intent_source="env",
            trade_recorder=lambda **kw: 1,
        )
        svc.config.universe = ["KOTAKBANK"]
        svc.sector_map = {"KOTAKBANK": "BANKNIFTY"}

        svc.run_entry()
        assert placed == []
        assert "KOTAKBANK" in svc.paper_book


# =========================================================================== #
# Scenario 4 — Stale feed: entries held + Telegram alert
# =========================================================================== #


class TestStaleFeedEntriesHeld:
    """data_freshness_service reports stale → run_entry aborts + alert fired;
    run_exit() still proceeds (exits are never blocked by staleness)."""

    _STALE = {
        "NIFTY": {"ok": False, "last_date": "2026-06-12", "staleness_days": 9, "kind": "index"},
        "BANKNIFTY": {"ok": False, "last_date": "2026-06-12", "staleness_days": 9, "kind": "index"},
        "HDFCBANK": {"ok": False, "last_date": "2026-06-12", "staleness_days": 9, "kind": "stock"},
    }

    @staticmethod
    def _stale_checker(name, date, index_only=False):
        return False, TestStaleFeedEntriesHeld._STALE

    def test_run_entry_returns_empty(self):
        svc, placed, _ = _make_service(
            _full_agg(),
            _full_hist(),
            mode="sandbox",
            data_health_checker=self._stale_checker,
            trade_recorder=lambda **kw: 1,
        )
        assert svc.run_entry() == []
        assert placed == []

    def test_run_entry_fires_telegram_alert(self):
        alerts: list = []
        svc, _, _ = _make_service(
            _full_agg(),
            _full_hist(),
            mode="sandbox",
            data_health_checker=self._stale_checker,
            notifier=lambda m: alerts.append(m),
            trade_recorder=lambda **kw: 1,
        )
        svc.run_entry()
        assert any("ABORTED" in a for a in alerts), f"Expected an ABORTED alert, got: {alerts}"

    def test_stale_abort_does_not_write_journal_rows(self, _isolate_journal_db):
        """No sector_follow_trades rows when entries are aborted by stale data."""
        svc, _, _ = _make_service(
            _full_agg(),
            _full_hist(),
            mode="sandbox",
            data_health_checker=self._stale_checker,
        )
        svc.run_entry()
        assert _isolate_journal_db.SectorFollowTrade.query.all() == []

    def test_run_exit_proceeds_despite_stale_index(self):
        """Exits must never be blocked — a held T+1 position is riskier to leave
        open than to square off on a slightly stale read."""
        from services.sector_follow_service import PaperPosition

        svc, placed, _ = _make_service(
            _full_agg(),
            _full_hist(),
            mode="sandbox",
            data_health_checker=self._stale_checker,
            trade_recorder=lambda **kw: 1,
        )
        yesterday = (
            (datetime(2026, 6, 23, 15, 20, tzinfo=_IST) - timedelta(days=1)).date().isoformat()
        )
        svc.paper_book["HDFCBANK"] = PaperPosition(
            symbol="HDFCBANK",
            quantity=10,
            entry_price=100.0,
            entry_date=yesterday,
            vol_ratio=2.5,
        )
        exited = svc.run_exit()
        assert len(exited) == 1
        assert exited[0]["symbol"] == "HDFCBANK"
        sell_orders = [o for o in placed if o[1].get("action") == "SELL"]
        assert len(sell_orders) == 1

    def test_feature_flag_off_skips_freshness_check(self, monkeypatch):
        """DATA_FRESHNESS_VALIDATION_ENABLED=false bypasses the stale gate."""
        monkeypatch.setenv("DATA_FRESHNESS_VALIDATION_ENABLED", "false")
        svc, placed, _ = _make_service(
            _full_agg(),
            _full_hist(),
            mode="sandbox",
            data_health_checker=self._stale_checker,
            trade_recorder=lambda **kw: 1,
        )
        # With the flag off, the stale checker is still injected but data_freshness_enabled()
        # returns False so the gate is skipped — entries should proceed.
        result = svc.run_entry()
        assert len(result) > 0, "Expected entries to proceed when flag is off"


# =========================================================================== #
# Scenario 5 — Smoke check abort: 15:18 override → 15:20 entry blocked
# =========================================================================== #


class TestSmokeCheckAbortBlocksEntry:
    """15:18 IST: assert_data_pipeline_healthy() returns False (aggregator empty)
    → writes a strategy_runtime_override pause row expiring at 15:30 IST
    → 15:20 run_entry() sees the override and returns [].

    Far-future dates (2099) ensure the override expiry is always in the future
    relative to the real wall clock, so the blocked-entry assertion holds
    regardless of when the test suite runs.
    """

    _NOW_SMOKE = datetime(2099, 6, 23, 15, 18, tzinfo=_IST)
    _NOW_ENTRY = datetime(2099, 6, 23, 15, 20, tzinfo=_IST)

    def _hist(self) -> dict:
        # History is present (historify probe ok); only aggregator is empty.
        return _full_hist()

    def test_smoke_check_fails_when_aggregator_empty(self):
        svc, _, _ = _make_service(
            aggregator_data={},
            history_data=self._hist(),
            mode="sandbox",
            trade_recorder=lambda **kw: 1,
            now_dt=self._NOW_SMOKE,
        )
        ok, details = svc.assert_data_pipeline_healthy()
        assert ok is False
        assert details["aggregator_ok"] is False

    def test_smoke_check_writes_pause_override_to_db(self, _isolate_sro_db):
        svc, _, _ = _make_service(
            aggregator_data={},
            history_data=self._hist(),
            mode="sandbox",
            trade_recorder=lambda **kw: 1,
            now_dt=self._NOW_SMOKE,
        )
        svc.assert_data_pipeline_healthy()
        rows = _isolate_sro_db.list_overrides(include_expired=True)
        pauses = [r for r in rows if r["override_type"] == "pause"]
        assert pauses, f"expected a pause override, got {rows}"
        assert any("smoke_check_failed" in (r["reason"] or "") for r in pauses)

    def test_smoke_check_fires_telegram_alert(self):
        alerts: list = []
        svc, _, _ = _make_service(
            aggregator_data={},
            history_data=self._hist(),
            mode="sandbox",
            notifier=lambda m: alerts.append(m),
            trade_recorder=lambda **kw: 1,
            now_dt=self._NOW_SMOKE,
        )
        svc.assert_data_pipeline_healthy()
        assert any("SMOKE CHECK FAILED" in a for a in alerts)

    def test_pause_override_expires_at_1530_ist(self, _isolate_sro_db):
        """The pause override must self-clear at 15:30 IST (not end-of-day),
        so a stale-aggregator blip never silently disables the strategy
        beyond the entry window."""
        import datetime as _dt

        svc, _, _ = _make_service(
            aggregator_data={},
            history_data=self._hist(),
            mode="sandbox",
            trade_recorder=lambda **kw: 1,
            now_dt=self._NOW_SMOKE,
        )
        svc.assert_data_pipeline_healthy()
        rows = _isolate_sro_db.list_overrides(include_expired=True)
        pauses = [
            r
            for r in rows
            if r["override_type"] == "pause" and "smoke_check_failed" in (r["reason"] or "")
        ]
        assert pauses
        # expires_at is stored as naive UTC; convert back to IST for the assertion.
        exp_utc = pauses[0]["expires_at"]
        if isinstance(exp_utc, str):
            exp_utc = _dt.datetime.fromisoformat(exp_utc)
        exp_ist = exp_utc + _dt.timedelta(hours=5, minutes=30)
        assert exp_ist.hour == 15, f"Expected expiry at 15:xx IST, got {exp_ist}"
        assert exp_ist.minute == 30, f"Expected expiry at 15:30 IST, got {exp_ist}"

    def test_15_20_entry_blocked_after_smoke_check_pause(self, _isolate_sro_db):
        """Full two-step integration: smoke check at 15:18 writes pause; a new
        service instance at 15:20 sees the DB row and returns 0 entries even
        though the aggregator now has data."""
        # Step 1 — 15:18 smoke check writes the pause row.
        svc_smoke, _, _ = _make_service(
            aggregator_data={},
            history_data=self._hist(),
            mode="sandbox",
            trade_recorder=lambda **kw: 1,
            now_dt=self._NOW_SMOKE,
        )
        ok, _ = svc_smoke.assert_data_pipeline_healthy()
        assert ok is False

        # Step 2 — 15:20 entry job fires (new instance, same DB via monkeypatch).
        # The aggregator has recovered (data present), but the pause row persists.
        agg_recovered = _full_agg()
        svc_entry, placed, _ = _make_service(
            aggregator_data=agg_recovered,
            history_data=self._hist(),
            mode="sandbox",
            trade_recorder=lambda **kw: 1,
            now_dt=self._NOW_ENTRY,
        )
        result = svc_entry.run_entry()
        assert result == [], "Entry should be blocked by the smoke-check pause override"
        assert placed == []

    def test_smoke_check_passes_when_aggregator_has_majority_coverage(self):
        """Aggregator has data for all symbols → smoke check passes, no override."""
        svc, _, _ = _make_service(
            aggregator_data=_full_agg(),
            history_data=self._hist(),
            mode="sandbox",
            trade_recorder=lambda **kw: 1,
            now_dt=self._NOW_SMOKE,
        )
        ok, details = svc.assert_data_pipeline_healthy()
        assert ok is True
        assert details["aggregator_ok"] is True
        assert details["historify_ok"] is True
