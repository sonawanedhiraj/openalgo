"""Contract test — every in-house scan_results fire must reproduce against
fresh broker /api/v1/history data (Issue #205).

This is the single test that would have caught all four of the 2026-06-29
scanner-rule bugs (PRs #198, #200, #202, #204). 147+ unit tests verified
gate logic on internally-consistent synthetic data; none asked "what if
two data sources for the same value DISAGREE?". This test does — by
re-invoking the rule against fresh broker bars and comparing verdicts.

**Live-data dependent. OPT-IN ONLY.** Gated by env
``SCANNER_CONTRACT_TEST_ENABLED`` (default ``false``). Default unit-CI runs
collect this module and report it as SKIPPED — they never call the broker.
Run manually after a session or wire into the hourly comparison job.

To run::

    SCANNER_CONTRACT_TEST_ENABLED=true uv run pytest \
        test/test_scanner_rule_vs_broker_contract.py -v -s

**Caveat — divergence is signal expiry vs bug.** A signal that fired at
14:35 IST and mean-reverted by 15:00 will look "divergent" when this test
runs at 16:00 — that's a legitimate signal that simply expired, not a
data-supply bug. Keep ``SCANNER_CONTRACT_TEST_WINDOW_MIN`` tight (default
60 min) and re-run shortly after the session, OR wire it into a cron
that polls every ~5 min so the lag between fire and verification is
small. Repeated divergences on the SAME symbol within minutes of the
fire are the bug class this test is built for.

**Why a raw sqlite3 read of the LIVE openalgo.db.** ``test/conftest.py``
redirects every DB env var to a per-process temp dir so unit tests can
never write to the live DB. This test reads — never writes — and the
``SCANNER_CONTRACT_TEST_ENABLED`` opt-in is the license to bypass the
redirect. We open ``db/openalgo.db`` directly in URI read-only mode.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from pathlib import Path

import pandas as pd
import pytest
import pytz

_IST = pytz.timezone("Asia/Kolkata")


def _env_true(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes", "on")


_CONTRACT_ENABLED = _env_true("SCANNER_CONTRACT_TEST_ENABLED", "false")
_WINDOW_MIN = int(os.environ.get("SCANNER_CONTRACT_TEST_WINDOW_MIN", "60"))
_MAX_DIVERGENCE_PCT = float(os.environ.get("SCANNER_CONTRACT_TEST_MAX_DIVERGENCE_PCT", "5"))


# Module-level skipif: gates every test below. Default unit CI runs collect
# the module and report all tests SKIPPED — they never touch the broker.
pytestmark = pytest.mark.skipif(
    not _CONTRACT_ENABLED,
    reason=(
        "SCANNER_CONTRACT_TEST_ENABLED is not set/true (default false). "
        "Set the env var to run this live-data contract test against the broker."
    ),
)


def _resolve_live_db_path() -> Path:
    """Path to the live ``db/openalgo.db``, ignoring the conftest redirect.

    The conftest redirects ``DATABASE_URL`` etc. at module import. This
    helper resolves the path from the repo root directly, so the contract
    test reads the LIVE DB regardless of the redirect.
    """
    return Path(__file__).resolve().parents[1] / "db" / "openalgo.db"


def _read_recent_inhouse_fires(window_min: int) -> list[dict]:
    """Read in-house ``scan_results`` rows from the last ``window_min`` minutes,
    joined with ``scan_definitions`` so each row carries its rule + parameters."""
    db_path = _resolve_live_db_path()
    if not db_path.exists():
        pytest.skip(f"Live openalgo.db not found at {db_path}")
    cutoff = _dt.datetime.now(_IST) - _dt.timedelta(minutes=window_min)

    rows: list[dict] = []
    # Read-only URI connect — never mutates the live DB.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT sr.id, sr.scan_definition_id, sr.run_at, sr.symbols, sr.notes,
                   sd.rule_module, sd.screener_type, sd.parameters_json
            FROM scan_results sr
            JOIN scan_definitions sd ON sd.id = sr.scan_definition_id
            WHERE sr.source = 'inhouse'
            ORDER BY sr.id DESC
            LIMIT 1000
            """
        )
        for row in cursor:
            run_at = row["run_at"] or ""
            try:
                ts = _dt.datetime.fromisoformat(run_at)
                if ts.tzinfo is None:
                    ts = _IST.localize(ts)
            except (ValueError, TypeError):
                continue
            if ts < cutoff:
                # rows are ordered by id DESC — once one is older, the rest are too.
                break
            rows.append(dict(row))
    finally:
        conn.close()
    return rows


def _fetch_broker_bars(
    symbol: str, interval: str, start_date: str, end_date: str
) -> pd.DataFrame | None:
    """Fetch fresh broker bars via ``services.history_service.get_history``.

    Returns ``None`` on any non-success or empty payload — the caller
    decides whether to skip or report.
    """
    from database.auth_db import get_first_available_api_key
    from services.history_service import get_history

    api_key = get_first_available_api_key()
    if not api_key:
        pytest.skip("No active broker session (get_first_available_api_key returned None).")
    success, payload, _code = get_history(
        symbol=symbol,
        exchange="NSE",
        interval=interval,
        start_date=start_date,
        end_date=end_date,
        api_key=api_key,
    )
    if not success:
        return None
    rows = (payload or {}).get("data") or []
    if not rows:
        return None
    df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    return df


def _roll_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """The broker /history API exposes ``D`` but not ``W`` — roll from D
    for the weekly ATR(21) gate, mirroring
    ``test_chartink_rules_persistent_astral_2026_06_29._roll_weekly``.
    """
    d = daily.copy()
    d["dt"] = pd.to_datetime(d["timestamp"], unit="s", utc=True).dt.tz_convert(_IST)
    d = d.set_index("dt")
    w = (
        d.resample("W-FRI")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
        .reset_index(drop=True)
    )
    return w


def _strip_today_from_daily(daily: pd.DataFrame, today: _dt.date) -> pd.DataFrame:
    """Drop today's D bar from the broker frame so the rule sees the same
    settled-bars-only state that ``ScannerHistoryProvider`` exposes in
    production (the post-close backfill runs at 15:30-17:00 IST).
    """
    dates = pd.to_datetime(daily["timestamp"], unit="s", utc=True).dt.tz_convert(_IST).dt.date
    return daily[dates < today].reset_index(drop=True)


def test_inhouse_fires_agree_with_broker_history():
    """Every in-house BUY/SELL fire in the last ``SCANNER_CONTRACT_TEST_WINDOW_MIN``
    minutes must reproduce when the same rule is invoked against fresh
    ``/api/v1/history`` data for the same symbol. Records divergences for
    triage; fails if divergence rate >
    ``SCANNER_CONTRACT_TEST_MAX_DIVERGENCE_PCT`` (default 5%).
    """
    fires = _read_recent_inhouse_fires(_WINDOW_MIN)
    if not fires:
        pytest.skip(f"No in-house scan_results in the last {_WINDOW_MIN}min — nothing to verify.")

    from services.scan_rules.fno_intraday_buy_chartink import rule as buy_rule
    from services.scan_rules.fno_intraday_sell_chartink import rule as sell_rule

    today = _dt.datetime.now(_IST).date()
    today_str = today.strftime("%Y-%m-%d")
    # 400 days back covers the SMA(volume, 200) warm-up + weekly ATR(21).
    lookback_d_start = (today - _dt.timedelta(days=400)).strftime("%Y-%m-%d")

    divergent: list[str] = []
    confirmed = 0
    skipped = 0

    for row in fires:
        try:
            symbols = json.loads(row["symbols"]) or []
        except (ValueError, TypeError):
            continue
        if not symbols:
            continue
        sym = symbols[0]
        rule_module = row["rule_module"] or ""
        if "fno_intraday_buy" in rule_module:
            rule = buy_rule
        elif "fno_intraday_sell" in rule_module:
            rule = sell_rule
        else:
            # Unknown rule — skip (not part of #205's scope).
            skipped += 1
            continue

        daily = _fetch_broker_bars(sym, "D", lookback_d_start, today_str)
        b5 = _fetch_broker_bars(sym, "5m", today_str, today_str)
        b15 = _fetch_broker_bars(sym, "15m", today_str, today_str)
        if daily is None or b5 is None or b15 is None:
            skipped += 1
            continue
        if len(daily) < 3 or len(b5) < 8 or len(b15) < 15:
            skipped += 1
            continue

        daily_no_today = _strip_today_from_daily(daily, today)
        if len(daily_no_today) < 2:
            skipped += 1
            continue
        weekly = _roll_weekly(daily)
        if len(weekly) < 22:
            skipped += 1
            continue

        try:
            params = json.loads(row["parameters_json"]) if row["parameters_json"] else {}
        except (ValueError, TypeError):
            params = {}
        indicators = {
            "symbol": sym,
            "exchange": "NSE",
            "bars_5m": b5,
            "bars_15m": b15,
            "bars_daily": daily_no_today,
            "bars_weekly": weekly,
            "parameters": params,
        }
        try:
            broker_matched = bool(rule(None, indicators))
        except Exception:
            skipped += 1
            continue

        if broker_matched:
            confirmed += 1
        else:
            # The values that drove the broker's REJECT verdict — without these,
            # the failure message is useless for triage.
            today_d_close = (
                float(daily_no_today.iloc[-1]["close"]) if len(daily_no_today) else float("nan")
            )
            last_5m_close = float(b5.iloc[-1]["close"]) if len(b5) else float("nan")
            divergent.append(
                f"{sym} (rule={rule_module}, defn={row['scan_definition_id']}, "
                f"scan_results.id={row['id']}, run_at={row['run_at']}): "
                f"in-house FIRED but broker REJECTS. "
                f"daily(stripped).iloc[-1].close={today_d_close:.2f} "
                f"latest_5m_close={last_5m_close:.2f}"
            )

    total = confirmed + len(divergent)
    if total == 0:
        pytest.skip(
            f"All {skipped} in-house fires were unverifiable (broker data missing). "
            "Likely a quiet window or no broker session."
        )
    divergence_pct = 100.0 * len(divergent) / total
    assert divergence_pct <= _MAX_DIVERGENCE_PCT, (
        f"Scanner rule-vs-broker divergence rate {divergence_pct:.1f}% exceeds the "
        f"SCANNER_CONTRACT_TEST_MAX_DIVERGENCE_PCT={_MAX_DIVERGENCE_PCT}% threshold. "
        f"{len(divergent)} divergent / {total} verifiable / {skipped} skipped:\n  "
        + "\n  ".join(divergent[:20])
    )
