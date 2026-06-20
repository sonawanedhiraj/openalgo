#!/usr/bin/env bash
# local_ci.sh — reproduce the full CI/CD pipeline locally on the laptop.
#
# Runs the exact same gates the self-hosted runner runs, in the same order,
# fail-fast at each stage. Run this BEFORE pushing to catch issues without
# waiting for the runner queue.
#
# Usage:
#   bash scripts/ci/local_ci.sh            # all stages
#   bash scripts/ci/local_ci.sh lint       # lint only
#   bash scripts/ci/local_ci.sh test       # unit tests only
#   bash scripts/ci/local_ci.sh docker     # docker build only
#   bash scripts/ci/local_ci.sh e2e        # docker build + boot + e2e
#
# Prerequisites: uv, Docker Desktop running, authenticated gh CLI.
# See docs/TASK_CAPABILITIES.md §6 for background.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

banner() { echo ""; echo "══════════════════════════════════════════"; echo "  $*"; echo "══════════════════════════════════════════"; }
pass()   { echo "✅  $*"; }
fail()   { echo "❌  $*" >&2; exit 1; }

stage_lint() {
  banner "STAGE 1 · Lint (ruff + semgrep)"
  uv run ruff check . && pass "ruff check" || fail "ruff check failed — run: uv run ruff check . --fix"
  uv run ruff format --check . && pass "ruff format" || fail "ruff format failed — run: uv run ruff format ."
  uvx semgrep --config .semgrep/silent-drops.yml --severity ERROR \
    services/ blueprints/ sandbox/ restx_api/ \
    && pass "semgrep silent-drops" \
    || fail "semgrep found ERROR-level findings — fix before pushing"
}

stage_test() {
  banner "STAGE 2 · Unit + Integration Tests"
  uv run python -m pytest test/ \
    -n auto \
    --ignore=test/e2e \
    --ignore=test/test_bridge_server.py \
    --ignore=test/test_email_functionality.py \
    -v --tb=short \
    && pass "pytest" \
    || fail "tests failed"
}

stage_docker_build() {
  banner "STAGE 3 · Docker build"
  # Preflight — same check the CD job runs
  if ! docker version --format '{{.Server.Version}}' 2>/dev/null; then
    fail "Docker daemon not reachable — is Docker Desktop running?"
  fi
  AVAIL=$(df "$ROOT" --output=avail -BG 2>/dev/null | tail -1 | tr -d 'G ' || echo 999)
  [ "${AVAIL:-999}" -lt 5 ] && fail "Less than 5 GB disk free (${AVAIL}GB). Run: docker system prune -f"
  docker compose build --quiet && pass "docker compose build"
}

stage_e2e() {
  banner "STAGE 4 · E2E (boot container + pytest test/e2e)"
  # .env must exist; CD generates a throwaway one — replicate that here
  if [ ! -f .env ]; then
    cp .sample.env .env
    python -c "import secrets; print(secrets.token_hex(32))" | xargs -I{} sed -i "s|^APP_KEY = .*|APP_KEY = '{}'|" .env
    python -c "import secrets; print(secrets.token_hex(32))" | xargs -I{} sed -i "s|^API_KEY_PEPPER = .*|API_KEY_PEPPER = '{}'|" .env
    sed -i "s|^REDIRECT_URL = .*|REDIRECT_URL = 'http://127.0.0.1:5000/zerodha/callback'|" .env
    echo "(generated throwaway .env for E2E)"
  fi
  docker compose up -d
  echo "Waiting for app to be ready (up to 60s)..."
  for i in {1..30}; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/ 2>/dev/null || echo "000")
    if [ "$STATUS" = "200" ] || [ "$STATUS" = "302" ]; then
      pass "App ready (HTTP $STATUS)"; break
    fi
    echo "  [$i/30] waiting... (HTTP $STATUS)"; sleep 2
  done
  uv run python -m pytest test/e2e -v --tb=short && pass "e2e tests" || { docker compose down -v; fail "e2e tests failed"; }
  docker compose down -v && pass "cleanup"
}

ALL_STAGES="lint test docker e2e"
REQUESTED="${1:-all}"

case "$REQUESTED" in
  all)    stage_lint; stage_test; stage_docker_build; stage_e2e ;;
  lint)   stage_lint ;;
  test)   stage_test ;;
  docker) stage_docker_build ;;
  e2e)    stage_docker_build; stage_e2e ;;
  *)      echo "Usage: $0 [all|lint|test|docker|e2e]"; exit 1 ;;
esac

banner "ALL DONE ✅"
