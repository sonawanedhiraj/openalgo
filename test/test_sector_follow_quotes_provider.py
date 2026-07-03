"""Unit tests for the quotes-snapshot intraday provider (issue #332).

Covers ``services.sector_follow_service.make_quotes_intraday_provider`` and its
wiring into ``services.futures_follow_service.production_signal_evaluator`` via
``FUTURES_FOLLOW_INTRADAY_SOURCE``. Fully hermetic — no broker, no DuckDB: the
quote fetch, the aggregator provider, and the historify reader are all injected
or monkeypatched, mirroring test/test_sector_follow_service.py.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from services.sector_follow_service import (
    SectorFollowConfig,
    SectorFollowService,
    make_duckdb_metrics_provider,
    make_quotes_intraday_provider,
)

_IST = timezone(timedelta(hours=5, minutes=30))

_AS_OF = datetime(2026, 7, 3, 15, 20, tzinfo=_IST)


def _ist_epoch(y, mo, d, h, mi):
    """Epoch seconds for an IST wall-clock time (matches _ist_date's reading)."""
    return datetime(y, mo, d, h, mi, tzinfo=_IST).timestamp()


def _prior_days_history(close_prev=100.0, vol_a=800.0, vol_b=1200.0, today_bars=None):
    """Two prior trading days (Wed 07-01, Thu 07-02) + optional today (Fri 07-03)
    bars. avg_vol over the two prior days = (vol_a + vol_b)/2 = 1000;
    prior_close = close_prev."""
    rows = [
        (_ist_epoch(2026, 7, 1, 15, 29), close_prev - 1.0, vol_a),
        (_ist_epoch(2026, 7, 2, 15, 29), close_prev, vol_b),
    ]
    if today_bars:
        rows = list(rows) + list(today_bars)
    return rows


_UNIVERSE = ["AAA", "BBB"]
_SECTOR_MAP = {"AAA": "NIFTY", "BBB": "NIFTY"}

# Quote snapshot that makes AAA pass all three gates (stock +2% > 0.5%,
# sector +1.5% > 1%, vol_ratio 2 > 1) and BBB fail the stock gate (+0.2%).
_GOOD_QUOTES = {
    "AAA": (102.0, 2000.0),
    "BBB": (100.2, 500.0),
    "NIFTY": (101.5, 12345.0),
}

_HISTORY = {
    "AAA": _prior_days_history(close_prev=100.0),
    "BBB": _prior_days_history(close_prev=100.0),
    "NIFTY": _prior_days_history(close_prev=100.0),
}


class _CountingFetcher:
    """Stub quotes fetcher: records payloads, returns a canned snapshot."""

    def __init__(self, snapshot=None, exc=None):
        self.snapshot = dict(snapshot or {})
        self.exc = exc
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(list(payload))
        if self.exc is not None:
            raise self.exc
        return dict(self.snapshot)


def _config(**overrides) -> SectorFollowConfig:
    base = {"universe": list(_UNIVERSE), "strategy_id": 99}
    base.update(overrides)
    return SectorFollowConfig(**base)


# --------------------------------------------------------------------------- #
# (a) batched + memoized mapping
# --------------------------------------------------------------------------- #
def test_quotes_provider_one_batched_call_maps_ltp_and_volume():
    """N per-symbol calls hit exactly ONE batched fetch; stocks map to
    (ltp, cumulative_volume, "quotes")."""
    fetcher = _CountingFetcher(_GOOD_QUOTES)
    provider = make_quotes_intraday_provider(
        universe=_UNIVERSE,
        sector_map=_SECTOR_MAP,
        quotes_fetcher=fetcher,
        fallback=lambda s, a: (None, None),
    )
    assert provider("AAA", _AS_OF) == (102.0, 2000.0, "quotes")
    assert provider("BBB", _AS_OF) == (100.2, 500.0, "quotes")
    assert provider("NIFTY", _AS_OF) == (101.5, None, "quotes")  # index: volume None
    assert len(fetcher.payloads) == 1, "expected exactly one batched get_multiquotes call"
    payload = fetcher.payloads[0]
    # Stocks route to NSE; the mapped sector index routes to NSE_INDEX (the same
    # routing sector_follow_index_backfill uses — no new mapping invented).
    assert {"symbol": "AAA", "exchange": "NSE"} in payload
    assert {"symbol": "BBB", "exchange": "NSE"} in payload
    assert {"symbol": "NIFTY", "exchange": "NSE_INDEX"} in payload
    assert len(payload) == 3


# --------------------------------------------------------------------------- #
# (b) broker failure → fail-safe fallback chain, no raise, one call
# --------------------------------------------------------------------------- #
def test_quotes_provider_broker_error_falls_back_without_raising(caplog):
    """A raising fetch is memoized (still ONE batched attempt), never propagates,
    WARNs per hop, and degrades to the aggregator fallback per symbol."""
    fetcher = _CountingFetcher(exc=RuntimeError("broker down"))
    aggregator = {"AAA": (101.0, 1500.0)}  # aggregator covers AAA only
    provider = make_quotes_intraday_provider(
        universe=_UNIVERSE,
        sector_map=_SECTOR_MAP,
        quotes_fetcher=fetcher,
        fallback=lambda s, a: aggregator.get(s, (None, None)),
    )
    with caplog.at_level(logging.WARNING, logger="services.sector_follow_service"):
        assert provider("AAA", _AS_OF) == (101.0, 1500.0, "aggregator")
        assert provider("BBB", _AS_OF) == (None, None, "none")
        assert provider("NIFTY", _AS_OF) == (None, None, "none")
    assert len(fetcher.payloads) == 1, "failure must be memoized — no retry storm"
    assert any("no quote for AAA" in r.message for r in caplog.records)
    assert any("no quote for BBB" in r.message for r in caplog.records)


def test_quotes_failure_reaches_historify_via_compute_metrics(caplog):
    """Full chain quotes → aggregator → historify: with quotes down and the
    aggregator empty, today's data still comes from historify with the existing
    loud WARNING (intraday_source='historify')."""
    fetcher = _CountingFetcher(exc=RuntimeError("broker down"))
    provider = make_quotes_intraday_provider(
        universe=_UNIVERSE,
        sector_map=_SECTOR_MAP,
        quotes_fetcher=fetcher,
        fallback=lambda s, a: (None, None),  # aggregator empty too
    )
    history = {
        sym: _prior_days_history(
            close_prev=100.0, today_bars=[(_ist_epoch(2026, 7, 3, 15, 19), 102.0, 2000.0)]
        )
        for sym in ["AAA", "BBB", "NIFTY"]
    }
    metrics_provider = make_duckdb_metrics_provider(
        intraday_provider=provider,
        history_reader=lambda syms, ws: {s: list(history.get(s, [])) for s in syms},
    )
    with caplog.at_level(logging.WARNING, logger="services.sector_follow_service"):
        metrics = metrics_provider(_AS_OF, _UNIVERSE, _SECTOR_MAP, _config())
    assert metrics["AAA"]["intraday_source"] == "historify"
    assert metrics["AAA"]["current_price"] == 102.0
    assert any("falling back to historify" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# (c) index path: close-only, gates still computable
# --------------------------------------------------------------------------- #
def test_index_quote_close_only_feeds_sector_ret():
    """An index quote yields (ltp, None); sector_ret is computed from it."""
    fetcher = _CountingFetcher(_GOOD_QUOTES)
    provider = make_quotes_intraday_provider(
        universe=_UNIVERSE,
        sector_map=_SECTOR_MAP,
        quotes_fetcher=fetcher,
        fallback=lambda s, a: (None, None),
    )
    metrics_provider = make_duckdb_metrics_provider(
        intraday_provider=provider,
        history_reader=lambda syms, ws: {s: list(_HISTORY.get(s, [])) for s in syms},
    )
    metrics = metrics_provider(_AS_OF, _UNIVERSE, _SECTOR_MAP, _config())
    assert metrics["AAA"]["sector_ret"] == pytest.approx(0.015)  # 101.5/100 - 1
    assert metrics["AAA"]["stock_ret"] == pytest.approx(0.02)
    assert metrics["AAA"]["vol_ratio"] == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# (d) intraday_source="quotes" propagates and counts as LIVE for completeness
# --------------------------------------------------------------------------- #
def test_quotes_source_propagates_and_counts_live_for_completeness():
    """Metrics carry intraday_source='quotes' and the completeness alerting
    treats quotes as live coverage (no WARNING/CRITICAL Telegram)."""
    from services.mode_service import EffectiveDecision

    fetcher = _CountingFetcher(_GOOD_QUOTES)
    provider = make_quotes_intraday_provider(
        universe=_UNIVERSE,
        sector_map=_SECTOR_MAP,
        quotes_fetcher=fetcher,
        fallback=lambda s, a: (None, None),
    )
    alerts = []
    svc = SectorFollowService(
        config=_config(),
        sector_map=dict(_SECTOR_MAP),
        mode="scaffold",
        intraday_provider=provider,
        history_reader=lambda syms, ws: {s: list(_HISTORY.get(s, [])) for s in syms},
        broker_session_checker=lambda: True,
        order_placer=lambda mode, order: {"status": "success", "orderid": "X"},
        price_fetcher=lambda s, e: None,
        notifier=lambda msg: alerts.append(msg),
        trade_recorder=lambda **kw: 1,
        now=lambda: _AS_OF,
        intent_resolver=lambda: EffectiveDecision(
            mode="sandbox", intent="run", daily_capital_cap=None, source="env"
        ),
    )
    metrics = svc._metrics_provider(_AS_OF, svc.config.universe, svc.sector_map, svc.config)
    assert metrics["AAA"]["intraday_source"] == "quotes"
    assert metrics["BBB"]["intraday_source"] == "quotes"
    candidates = svc.evaluate_candidates(_AS_OF)
    assert [c["symbol"] for c in candidates] == ["AAA"]
    # 2/2 symbols on a live source → no completeness degradation alert.
    assert not any("WARNING" in a or "CRITICAL" in a for a in alerts), alerts


# --------------------------------------------------------------------------- #
# Evaluator wiring: FUTURES_FOLLOW_INTRADAY_SOURCE flag
# --------------------------------------------------------------------------- #
def _patch_sector_follow_world(monkeypatch, *, aggregator, history):
    """Point the sector_follow config/map/data world at hermetic fakes for
    futures_follow's production_signal_evaluator (which lazily imports them)."""
    import services.sector_follow_service as sf

    monkeypatch.setattr(sf, "load_config", lambda path=None: _config())
    monkeypatch.setattr(sf, "load_sector_map", lambda path=None: dict(_SECTOR_MAP))
    monkeypatch.setattr(
        sf,
        "production_intraday_provider",
        lambda sym, as_of: aggregator.get(sym, (None, None)),
    )
    monkeypatch.setattr(
        sf,
        "production_history_reader",
        lambda syms, ws, db_path="db/historify.duckdb": {s: list(history.get(s, [])) for s in syms},
    )


def test_flag_aggregator_is_regression_identical_and_never_touches_quotes(monkeypatch):
    """(e) With FUTURES_FOLLOW_INTRADAY_SOURCE=aggregator the evaluator output is
    identical to the direct pre-#332 duckdb_metrics_provider path and the quotes
    stack is never invoked."""
    import services.sector_follow_service as sf
    from services.futures_follow_service import production_signal_evaluator
    from services.sector_follow_service import (
        duckdb_metrics_provider,
        passes_gates,
        select_entries,
    )

    aggregator = {"AAA": (102.0, 2000.0), "BBB": (100.2, 500.0), "NIFTY": (101.5, 0.0)}
    _patch_sector_follow_world(monkeypatch, aggregator=aggregator, history=_HISTORY)

    def _boom(payload):
        raise AssertionError("quotes fetch must not be called with source=aggregator")

    monkeypatch.setattr(sf, "_fetch_quotes_snapshot", _boom)
    monkeypatch.setenv("FUTURES_FOLLOW_INTRADAY_SOURCE", "aggregator")

    got = production_signal_evaluator(as_of=_AS_OF)

    # Expected: exactly what the pre-#332 code path computes.
    cfg = _config()
    metrics = duckdb_metrics_provider(_AS_OF, cfg.universe, dict(_SECTOR_MAP), cfg)
    expected = select_entries(
        [
            {
                "symbol": s,
                "vol_ratio": m.get("vol_ratio"),
                "stock_ret": m.get("stock_ret"),
                "sector_ret": m.get("sector_ret"),
            }
            for s, m in metrics.items()
            if passes_gates(m, cfg)
        ],
        set(),
        cfg.max_concurrent_positions,
    )
    assert got == expected
    assert [c["symbol"] for c in got] == ["AAA"]


def test_flag_quotes_survives_empty_aggregator_all_day(monkeypatch, caplog):
    """(f) The 2026-06-15 failure class: scanner aggregator empty all day, no
    today bars in historify — with quotes available the evaluator still produces
    the correct signal set, tagged source=quotes, with the INFO summary line."""
    import services.sector_follow_service as sf
    from services.futures_follow_service import production_signal_evaluator

    _patch_sector_follow_world(monkeypatch, aggregator={}, history=_HISTORY)
    fetcher = _CountingFetcher(_GOOD_QUOTES)
    monkeypatch.setattr(sf, "_fetch_quotes_snapshot", fetcher)
    monkeypatch.setenv("FUTURES_FOLLOW_INTRADAY_SOURCE", "quotes")

    with caplog.at_level(logging.INFO, logger="services.futures_follow_service"):
        got = production_signal_evaluator(as_of=_AS_OF)

    assert [c["symbol"] for c in got] == ["AAA"]
    assert got[0]["vol_ratio"] == pytest.approx(2.0)
    assert len(fetcher.payloads) == 1, "one batched quote call per eval cycle"
    summary = [
        r.message for r in caplog.records if "futures_follow intraday source=quotes" in r.message
    ]
    assert summary, "expected the per-eval INFO source summary line"
    assert "fetched=2/2" in summary[0]
    assert "fallback_aggregator=0" in summary[0]
    assert "fallback_historify=0" in summary[0]
    assert "none=0" in summary[0]


def test_flag_quotes_broker_down_falls_back_to_aggregator(monkeypatch, caplog):
    """(b/AC-3 at the evaluator level) Broker session down at 15:20: no raise,
    the eval degrades quotes → aggregator and still produces the signal set."""
    import services.sector_follow_service as sf
    from services.futures_follow_service import production_signal_evaluator

    aggregator = {"AAA": (102.0, 2000.0), "BBB": (100.2, 500.0), "NIFTY": (101.5, 0.0)}
    _patch_sector_follow_world(monkeypatch, aggregator=aggregator, history=_HISTORY)
    monkeypatch.setattr(sf, "_fetch_quotes_snapshot", lambda payload: {})  # no session
    monkeypatch.setenv("FUTURES_FOLLOW_INTRADAY_SOURCE", "quotes")

    # A single at_level: the caplog handler sits at INFO, so the futures INFO
    # summary AND the sector_follow fallback WARNINGs are both captured.
    with caplog.at_level(logging.INFO, logger="services.futures_follow_service"):
        got = production_signal_evaluator(as_of=_AS_OF)

    assert [c["symbol"] for c in got] == ["AAA"]
    warn = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "falling back to aggregator" in r.message
    ]
    assert warn, "expected a WARNING per fallback hop"
    summary = [
        r.message for r in caplog.records if "futures_follow intraday source=quotes" in r.message
    ]
    assert summary and "fetched=0/2" in summary[0] and "fallback_aggregator=2" in summary[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
