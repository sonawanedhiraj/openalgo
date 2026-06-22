#!/usr/bin/env bash
# count_tests.sh — emit JSON with unit + integration test counts.
#
# Single source of truth for "how do we count tests" — invoked by both the
# PR-time gate (.github/workflows/pr-test-count.yml) and the dev-push baseline
# updater (.github/workflows/update-test-baseline.yml). Unit and integration
# (E2E) are counted separately so a deletion in one bucket can't be hidden by
# additions in the other.
#
# Counts ITEMS COLLECTED by pytest, not files. A `@pytest.mark.parametrize`
# row counts as one item — which matches operator intuition: "how many test
# cases would actually run?"
#
# Matches the test-classification convention `.github/workflows/ci-cd.yml`
# already uses:
#   * Unit       = `test/` minus `test/e2e/` minus the bridge / email shims
#   * Integration = `test/e2e/`
#
# Output (stdout): a single JSON object with three keys. stderr carries the
# raw pytest output for debugging.
#
# Exit non-zero only if BOTH collections fail — never on a zero count alone
# (a project could legitimately have zero E2E tests at some point).
set -euo pipefail

# These ignores match what `.github/workflows/ci-cd.yml`'s CI stage already
# excludes. Keep them in lockstep.
UNIT_IGNORES=(
    --ignore=test/e2e
    --ignore=test/test_bridge_server.py
    --ignore=test/test_email_functionality.py
)

count_collected() {
    # $@ is passed to pytest. Echoes the integer count, or 0 on failure.
    local out exit_code
    out=$(uv run python -m pytest --collect-only -q "$@" 2>&1) || exit_code=$?
    exit_code=${exit_code:-0}
    # pytest's summary line is `"N tests collected"` (or "1 test collected" / "N
    # tests collected in Xs"). Grab the last such line.
    local n
    n=$(printf '%s\n' "$out" | grep -oE '[0-9]+ tests? collected' | tail -1 \
        | grep -oE '^[0-9]+' || true)
    if [ -z "${n:-}" ]; then
        printf 'count_tests: collection failed for %s\n' "$*" >&2
        printf '%s\n' "$out" | tail -20 >&2
        n=0
    fi
    printf '%s' "$n"
}

unit_n=$(count_collected "${UNIT_IGNORES[@]}" test/)
e2e_n=$(count_collected test/e2e)

if [ "$unit_n" = "0" ] && [ "$e2e_n" = "0" ]; then
    echo "count_tests: both unit and e2e collected zero — failing" >&2
    exit 1
fi

cat <<JSON
{
  "unit_count": $unit_n,
  "integration_count": $e2e_n,
  "counted_at_iso": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
