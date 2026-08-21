"""Watched vs tradeable: collect on everything, act only on F&O (issue #648).

``SCANNER_SYMBOLS`` is hand-maintained and NSE moves stocks in and out of F&O on
its own schedule, so the list drifts. On **2026-08-19** it held 211 NSE names of
which three had no option contracts at all — EXIDEIND, NUVAMA and SAMMAANCAP,
whose last contracts expired 2026-06-30 and were never re-listed. The scanner
kept subscribing them, scoring them and evaluating F&O screener rules against
them.

The split this file pins:

* ``scanner_universe()``   -> DATA MAINTENANCE. Raw, unfiltered, forever.
* ``tradeable_universe()`` -> DECISIONS. Minus anything absent from the master
  contract.

**Keeping backfill on the raw list is the load-bearing half.** Drop a name from
data maintenance and its 1m/``D`` series freezes the day NSE excludes it; when
NSE adds it back the scanner's gap and volume gates read a series with a
months-wide hole, and the convergence check — which only fetches symbols behind
*today's* close — cannot heal it without the manual CLI. History is cheap.
"""

import pytest

WATCHED = {"CGPOWER", "MANKIND", "HAL", "SAMMAANCAP", "EXIDEIND", "NUVAMA"}
NO_CONTRACTS = {"SAMMAANCAP", "EXIDEIND", "NUVAMA"}
TRADEABLE = WATCHED - NO_CONTRACTS


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from services.scanner_universe import reset_cache

    monkeypatch.setenv("SCANNER_SYMBOLS", ",".join(sorted(WATCHED)))
    reset_cache()
    yield
    reset_cache()


def _underlyings(extra=()):
    """A plausible master contract — above the #647 plausibility floor."""
    return TRADEABLE | set(extra) | {f"FILLER{i}" for i in range(60)}


def _patch_dump(monkeypatch, unders):
    monkeypatch.setattr("services.option_liquidity_service.option_underlyings", lambda: unders)


# --------------------------------------------------------------------------- #
# 1. The two answers
# --------------------------------------------------------------------------- #
def test_tradeable_drops_names_with_no_option_contracts(monkeypatch):
    from services.scanner_universe import tradeable_universe

    _patch_dump(monkeypatch, _underlyings())

    assert tradeable_universe() == TRADEABLE


def test_scanner_universe_is_never_filtered(monkeypatch):
    """The half that makes re-inclusion gap-free.

    If data maintenance shrank with the tradeable set, a symbol NSE re-admits
    would come back with no history behind it — and the daily convergence check
    only fetches what is behind TODAY's close, so nothing would heal it.
    """
    from services.scanner_universe import scanner_universe

    _patch_dump(monkeypatch, _underlyings())

    assert scanner_universe() == WATCHED
    assert NO_CONTRACTS <= scanner_universe()


def test_re_inclusion_needs_no_code_or_config_change(monkeypatch):
    from services.scanner_universe import reset_cache, tradeable_universe

    _patch_dump(monkeypatch, _underlyings())
    assert "SAMMAANCAP" not in tradeable_universe()

    # NSE re-admits it; tomorrow's instrument dump carries it
    _patch_dump(monkeypatch, _underlyings(extra=["SAMMAANCAP"]))
    reset_cache()  # in production the date key rolls over instead

    assert "SAMMAANCAP" in tradeable_universe()


def test_the_reverse_direction_is_never_auto_added(monkeypatch):
    """The master contract's index underlyings (NIFTY, BANKNIFTY, ...) have
    options and are deliberately NOT in an equity scanner universe. This filter
    only ever removes."""
    from services.scanner_universe import tradeable_universe

    _patch_dump(monkeypatch, _underlyings(extra=["NIFTY", "BANKNIFTY", "FINNIFTY"]))

    assert tradeable_universe() == TRADEABLE


# --------------------------------------------------------------------------- #
# 2. Fail OPEN — never dark the scanner
# --------------------------------------------------------------------------- #
def test_an_implausible_instrument_dump_keeps_everything(monkeypatch):
    from services.scanner_universe import tradeable_universe

    _patch_dump(monkeypatch, {"ONLYONE"})

    assert tradeable_universe() == WATCHED


def test_a_raising_filter_keeps_everything(monkeypatch):
    import services.scanner_universe as su

    def boom(*a, **k):
        raise RuntimeError("import blew up")

    monkeypatch.setattr("services.fno_universe.filter_to_fno", boom)

    assert su.tradeable_universe() == WATCHED


def test_an_empty_env_is_empty_not_an_error(monkeypatch):
    from services.scanner_universe import reset_cache, scanner_universe, tradeable_universe

    monkeypatch.setenv("SCANNER_SYMBOLS", "")
    reset_cache()
    _patch_dump(monkeypatch, _underlyings())

    assert scanner_universe() == set() and tradeable_universe() == set()


# --------------------------------------------------------------------------- #
# 3. The day cache
# --------------------------------------------------------------------------- #
def test_the_dump_is_read_once_per_day(monkeypatch):
    from services.scanner_universe import tradeable_universe

    calls = []

    def counted():
        calls.append(1)
        return _underlyings()

    monkeypatch.setattr("services.option_liquidity_service.option_underlyings", counted)
    tradeable_universe()
    tradeable_universe()
    tradeable_universe()

    assert len(calls) == 1, "every scanner tick must not hit the master contract"


def test_a_new_day_rebuilds(monkeypatch):
    import datetime as dt

    from services.scanner_universe import tradeable_universe

    _patch_dump(monkeypatch, _underlyings())
    assert "SAMMAANCAP" not in tradeable_universe(today=dt.date(2026, 8, 19))

    _patch_dump(monkeypatch, _underlyings(extra=["SAMMAANCAP"]))
    assert "SAMMAANCAP" in tradeable_universe(today=dt.date(2026, 8, 20))


def test_an_env_edit_is_not_masked_by_the_cache(monkeypatch):
    from services.scanner_universe import tradeable_universe

    _patch_dump(monkeypatch, _underlyings())
    assert tradeable_universe() == TRADEABLE

    monkeypatch.setenv("SCANNER_SYMBOLS", "HAL,CGPOWER")
    assert tradeable_universe() == {"HAL", "CGPOWER"}


# --------------------------------------------------------------------------- #
# 4. The WIRING — asserted per module, not by reading the source
# --------------------------------------------------------------------------- #
def test_the_aggregator_universe_is_the_tradeable_one(monkeypatch):
    import services.scanner_aggregator_symbols as agg

    _patch_dump(monkeypatch, _underlyings())
    syms = set(agg._scanner_symbols())

    assert syms == TRADEABLE
    assert not (syms & NO_CONTRACTS), "a WS slot spent on a name we can never act on"


def test_the_smoke_check_measures_coverage_over_the_tradeable_set(monkeypatch):
    """Counting names with no contracts against the coverage threshold would
    drag the fraction down and hold the scanner for a gap that cannot matter."""
    import services.scanner_smoke_check_service as smoke

    _patch_dump(monkeypatch, _underlyings())

    assert set(smoke.production_universe_provider()) == TRADEABLE


def test_the_rules_history_provider_reads_the_tradeable_set(monkeypatch):
    import services.scanner_history_provider as shp

    _patch_dump(monkeypatch, _underlyings())
    monkeypatch.setattr(shp, "_default_provider", None)
    prov = shp.get_provider()

    assert set(prov.symbols) == TRADEABLE


def test_the_backfill_keeps_maintaining_dropped_names(monkeypatch):
    """The inverse assertion, and the one most likely to be 'tidied' away."""
    import services.scanner_universe_backfill as bf

    _patch_dump(monkeypatch, _underlyings())
    syms = set(bf.scanner_universe_symbols())

    assert syms == WATCHED
    assert NO_CONTRACTS <= syms, "backfill must not shrink with the tradeable set"


def test_the_liveness_heal_resubscribes_the_tradeable_set(monkeypatch):
    """A heal that re-subscribes the RAW list would silently re-widen the
    universe every time the watchdog fires."""
    import services.tick_liveness_watchdog as tlw

    _patch_dump(monkeypatch, _underlyings())
    seen = {}

    class _Sub:
        def ensure(self, user_id, broker, symbols, reset=False):
            seen["symbols"] = set(symbols)
            return len(symbols)

    monkeypatch.setattr("services.scanner_presubscribe.scanner_pre_subscriber", _Sub())
    monkeypatch.setattr(tlw, "_resolve_session", lambda: ("u1", "zerodha"))
    assert tlw.heal_step_resubscribe() is True
    assert seen["symbols"] == TRADEABLE
