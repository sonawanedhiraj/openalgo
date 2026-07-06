"""Cross-source divergence contract tests (issue #225).

Every recent in-house-scanner regression has been a *cross-source
consistency* bug: the service reads from two sources for the same value,
the sources disagree mid-session, and the service picks the stale one
silently. PR #198 / #200 / #202 / #204 each shipped a fix paired with a
unit test that asserted rule logic on a single internally-consistent
synthetic frame — none of them asked "what if source A says X and
source B says Y?". This package fills that gap.

Each module here pins one multi-source service. The pattern is
deliberately uniform so a new multi-source service can copy-paste:

  1. Build two synthetic sources for the same value.
  2. Make them DISAGREE on a concrete number (stale vs fresh).
  3. Drive the service.
  4. Assert it picked the live/fresh source OR raised an observable
     divergence signal (logger.warning, return-value flag, etc).

The bug class is "silent stale-source pick" — never "live unavailable".

Service coverage:

* ``test_scanner_source_consistency.py``    — pins
  ``services.scan_rules._today_running.derive_today_and_yest``. This is
  the function PR #204 introduced precisely to fix the
  frozen-historify-vs-live-5m bug. The contract test seeds historify with
  a stale "today" bar and 5m with a fresher series; assertion is that the
  derived ``today_d.close`` reflects the live 5m close, not the stale D.

* ``test_freshness_source_consistency.py``  — pins
  ``services.data_freshness_service.compute_stale_symbols``. Two probes:
  (1) a row 5 business days old is correctly flagged stale; (2) a symbol
  with NO bars (never ingested) is NOT silently treated as fresh — the
  function must distinguish "never ingested" from "fresh".

* ``test_bar_seeder_source_consistency.py`` — pins
  ``services.scanner_aggregator_seeder._read_1m_bars_for_symbol``. The
  two-tier reader (historify → broker fallback) must observably log the
  source choice when it falls through to the broker. Asserts the
  fallback fires + emits ``logger.info`` so the source pick is visible
  in errors.jsonl / log files.

* ``test_eod_reconciliation_source_consistency.py`` — pins
  ``services.engine_eod_reconciliation_service.reconcile_engine_journal``.
  The trade_journal + sandbox.db are two views of the same trade. When
  sandbox has flattened a position via MIS auto-square-off but the journal
  still shows it open, reconcile must close the journal row (it is the
  divergence-resolution path). When the position is still open in
  sandbox, reconcile must NOT silently close it.
"""
