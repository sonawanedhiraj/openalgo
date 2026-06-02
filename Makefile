# Developer entry points. Used locally before pushing and in CI on PRs.

UV_RUN := uv run

.PHONY: smoke smoke-live test test-engine lint fmt gate help

help:
	@echo "Targets:"
	@echo "  make smoke       — import-only smoke (no running app required)"
	@echo "  make smoke-live  — running-app probe (requires OpenAlgo + broker session)"
	@echo "  make test        — full pytest suite"
	@echo "  make test-engine — only engine + journal tests"
	@echo "  make lint        — ruff check"
	@echo "  make fmt         — ruff format (mutates files)"
	@echo "  make gate        — lint + smoke + engine tests (CI pre-merge bundle)"

smoke:
	$(UV_RUN) python scripts/smoke_boot.py

smoke-live:
	$(UV_RUN) python scripts/smoke_engine_live.py

test:
	$(UV_RUN) pytest test/ -q --ignore=test/test_editor_strategy.py

test-engine:
	$(UV_RUN) pytest test/test_simplified_stock_engine_service.py test/test_engine_journal_integration.py test/test_eod_watchdog_service.py test/test_trade_journal_service.py -q

# Scoped to the smoke scripts this dev-tooling round owns. A repo-wide
# `ruff check .` reports ~1500 pre-existing errors in broker/legacy modules,
# and even `scripts/` alone has pre-existing errors in older bench/render
# helpers. The CI `backend-lint` job runs the full sweep as a non-blocking
# advisory; scoping here keeps `gate` a meaningful green/red signal for the
# new code instead of being permanently red on legacy debt.
SMOKE_SRC := scripts/smoke_boot.py scripts/smoke_engine_live.py

lint:
	$(UV_RUN) ruff check $(SMOKE_SRC)

fmt:
	$(UV_RUN) ruff format $(SMOKE_SRC)

# Pre-merge bundle. Uses the engine subset (not the full `test` target):
# the full 942-test suite includes broker-integration tests that need live
# credentials / network and are not CI-safe — the repo's existing
# `backend-test` CI job runs only a curated subset for the same reason.
# `test-engine` (103 tests, ~30s) is fast and reliably green, so `gate`
# stays a meaningful green/red signal. Run `make test` for the full suite.
gate: lint smoke test-engine
	@echo "✓ pre-merge gate passed"
