#!/usr/bin/env bash
# Bootstrap the GitHub label taxonomy for the task-tracking workflow.
#
# Idempotent: `gh label create --force` updates a label if it already exists,
# so this is safe to re-run. Requires an authenticated `gh` CLI.
#
# Usage:  bash scripts/gh/bootstrap_labels.sh
#
# See docs/TASK_CAPABILITIES.md and the "Task Tracking" section of CLAUDE.md
# for how these labels are applied.
set -euo pipefail

create() {
  # $1=name  $2=color(hex, no #)  $3=description
  gh label create "$1" --color "$2" --description "$3" --force
}

echo "== Kind (type:*) — reuse existing, ensure present =="
create "type:bug"         "d73a4a" "A defect in existing behaviour"
create "type:enhancement" "a2eeef" "New feature or capability"
create "type:docs"        "0075ca" "Documentation only"
create "type:infra"       "0e8a16" "CI/CD, tooling, build, repo plumbing"
create "type:incident"    "b60205" "Live/operational incident"
create "type:strategy"    "5319e7" "Trading strategy work"

echo "== Lifecycle (status:*) — new =="
create "status:in-progress" "fbca04" "Actively being worked on"
create "status:blocked"     "d93f0b" "Waiting on a dependency or decision"
create "status:needs-review" "0e8a16" "Work done, awaiting review/merge"

echo "== Origin (session:*) — new, for auditing which session opened it =="
create "session:claude-code" "c5def5" "Opened by an interactive Claude Code session"
create "session:bridge"      "c5def5" "Opened by a bridge-spawned claude -p session"
create "session:cowork"      "c5def5" "Proposed by a Cowork dispatch (read-only) session"

echo "== Backtesting — new =="
create "strategy:backtest-round" "5319e7" "A tracked backtest round (report + registry entry)"

echo ""
echo "Done. Verify with: gh label list --limit 100"
