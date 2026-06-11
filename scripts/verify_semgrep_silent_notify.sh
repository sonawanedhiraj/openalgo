#!/usr/bin/env bash
# E2E verification for the `silent-notification-on-liveness-only` Semgrep rule
# (2026-06-11 retrospective plan item #5).
#
# Asserts the rule:
#   1. FIRES on the pre-fix notify() (60a466f8^) — the Failure 2 silent drop.
#   2. Does NOT fire on the current (post-fix) notify().
#   3. Does NOT fire anywhere else across services/ blueprints/.
#
# Run from anywhere:  bash scripts/verify_semgrep_silent_notify.sh
# Requires: git, uvx (uv). Exit 0 = all assertions hold; non-zero = regression.
#
# Note on path scoping: the rule is scoped via paths.include (**/services/),
# and semgrep matches include globs against paths RELATIVE to the scan root —
# an absolute /tmp/... path does not match. So the pre-fix snapshot is written
# to a throwaway path *inside* services/ (cleaned up on exit) rather than /tmp.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CONFIG=".semgrep/silent-drops.yml"
RULE="silent-notification-on-liveness-only"
PREFIX_COMMIT="60a466f8^"
TARGET="services/notification_service.py"
PROBE="services/_semgrep_verify_silent_notify_probe.py"

cleanup() { rm -f "$PROBE"; }
trap cleanup EXIT

# Exact, severity-independent finding count for our rule under a path (we scan
# without --severity so a future ERROR->WARNING downgrade can't make this pass
# silently). check_id may be prefixed with the config path, so match on suffix.
count() {
  uvx semgrep --config "$CONFIG" "$1" --json --quiet 2>/dev/null \
    | uv run python -c "import sys,json;d=json.load(sys.stdin);print(sum(1 for r in d['results'] if r['check_id'].split('.')[-1]=='$RULE'))"
}

echo "== 1. pre-fix ($PREFIX_COMMIT:$TARGET) should FIRE =="
git show "$PREFIX_COMMIT:$TARGET" > "$PROBE"
PRE=$(count "$PROBE")
echo "   findings: $PRE"
[ "$PRE" -ge 1 ] || { echo "FAIL: rule did not fire on the pre-fix Failure 2 shape"; exit 1; }
cleanup

echo "== 2. post-fix (current $TARGET) should NOT fire =="
POST=$(count "$TARGET")
echo "   findings: $POST"
[ "$POST" -eq 0 ] || { echo "FAIL: rule fired on the fixed notify() — false positive"; exit 1; }

echo "== 3. whole tree (services/ blueprints/) should NOT fire =="
ALL=$(uvx semgrep --config "$CONFIG" services/ blueprints/ --json --quiet 2>/dev/null \
  | uv run python -c "import sys,json;d=json.load(sys.stdin);print(sum(1 for r in d['results'] if r['check_id'].split('.')[-1]=='$RULE'))")
echo "   findings: $ALL"
[ "$ALL" -eq 0 ] || { echo "FAIL: rule fired elsewhere in the tree — re-scope or fix the hit"; exit 1; }

echo
echo "PASS: rule fires on the Failure 2 shape ($PRE), silent on the fix (0), clean across the tree (0)."
