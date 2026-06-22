#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# docker_smoke.sh — Docker deployment smoke test for OpenAlgo.
#
# Part of the issue-completion Definition of Done (GitHub issue #6): builds the
# image, boots it in an ISOLATED container, verifies the app comes up healthy,
# scans the boot logs for regressions, then tears everything down.
#
# ISOLATION (so it never disturbs the live trading instance on :5000):
#   * own image tag        openalgo:smoketest   (never the live openalgo:latest)
#   * own container name    openalgo-smoketest   (compose uses openalgo-web)
#   * alt host ports        5055 -> 5000, 8799 -> 8765   (live keeps 5000/8765)
#   * generated throwaway   random APP_KEY/API_KEY_PEPPER, mode=sandbox, NO
#     .env                  broker credentials — so the container NEVER logs in
#                           to the broker and cannot touch the shared daily
#                           token of the live session.
#   * ephemeral DB          no named volumes mounted -> fresh db/ each run, gone
#                           on teardown (also exercises first-boot DB init).
#
# Usage:
#   scripts/docker_smoke.sh                # build + boot + check + teardown
#   BUILD=0 scripts/docker_smoke.sh        # skip build, reuse openalgo:smoketest
#   KEEP=1  scripts/docker_smoke.sh        # leave the container running for poking
#   HOST_HTTP_PORT=5056 scripts/docker_smoke.sh
#
# Exit code 0 = PASS, 1 = FAIL. Designed to run from Git Bash on the Windows
# laptop and on Linux/macOS.
# ---------------------------------------------------------------------------
set -uo pipefail

IMAGE="${IMAGE:-openalgo:smoketest}"
NAME="${CONTAINER_NAME:-openalgo-smoketest}"
HOST_HTTP_PORT="${HOST_HTTP_PORT:-5055}"
HOST_WS_PORT="${HOST_WS_PORT:-8799}"
BUILD="${BUILD:-1}"
KEEP="${KEEP:-0}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-150}"   # seconds to wait for the health endpoint
HEALTH_PATH="/auth/check-setup"        # same endpoint the compose healthcheck uses

# Resolve repo root (this script lives in <root>/scripts/).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PASS=0; FAIL=0
ok()   { echo "  [PASS] $1"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
info() { echo "==> $1"; }

ENV_FILE=""
cleanup() {
  if [ "$KEEP" = "1" ]; then
    info "KEEP=1 — leaving container '$NAME' running on http://127.0.0.1:${HOST_HTTP_PORT}"
  else
    info "Teardown: removing container '$NAME'"
    docker rm -f "$NAME" >/dev/null 2>&1 || true
  fi
  [ -n "$ENV_FILE" ] && rm -f "$ENV_FILE" 2>/dev/null || true
}
trap cleanup EXIT

# --- 0. Pre-flight: docker present, ports free, no name clash ----------------
info "Docker deployment smoke test for OpenAlgo"
command -v docker >/dev/null 2>&1 || { echo "docker not found in PATH"; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker daemon not reachable (is Docker Desktop running?)"; exit 1; }

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  info "Removing stale container '$NAME' from a previous run"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
fi

# --- 1. Generate a throwaway sandbox .env (NO broker creds) -------------------
info "Generating throwaway sandbox .env (random secrets, sandbox mode, no broker)"
gen_hex() {
  if command -v python >/dev/null 2>&1; then python -c "import secrets;print(secrets.token_hex(32))";
  elif command -v python3 >/dev/null 2>&1; then python3 -c "import secrets;print(secrets.token_hex(32))";
  else openssl rand -hex 32; fi
}
ENV_FILE="$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/openalgo_smoke_env.$$")"
cat > "$ENV_FILE" <<EOF
# Throwaway env for docker_smoke.sh — generated $(date -u +%FT%TZ). Do not reuse.
APP_KEY='$(gen_hex)'
API_KEY_PEPPER='$(gen_hex)'
DATABASE_URL='sqlite:///db/openalgo.db'
LATENCY_DATABASE_URL='sqlite:///db/latency.db'
LOGS_DATABASE_URL='sqlite:///db/logs.db'
HEALTH_DATABASE_URL='sqlite:///db/health.db'
SANDBOX_DATABASE_URL='sqlite:///db/sandbox.db'
HOST_SERVER='http://127.0.0.1:5000'
FLASK_HOST_IP='0.0.0.0'
FLASK_PORT='5000'
FLASK_DEBUG='False'
FLASK_ENV='production'
WEBSOCKET_HOST='0.0.0.0'
WEBSOCKET_PORT='8765'
VALID_BROKERS='zerodha'
SIMPLIFIED_ENGINE_MODE='sandbox'
LOG_TO_FILE='True'
EOF

# --- 2. Build (optional) -----------------------------------------------------
if [ "$BUILD" = "1" ]; then
  info "Building image '$IMAGE' (this can take several minutes: uv sync + frontend + chromium)"
  if ! docker build -t "$IMAGE" .; then bad "docker build"; echo; echo "RESULT: FAIL ($FAIL failed)"; exit 1; fi
  ok "docker build"
else
  info "BUILD=0 — reusing existing image '$IMAGE'"
  docker image inspect "$IMAGE" >/dev/null 2>&1 && ok "image '$IMAGE' present" || { bad "image '$IMAGE' missing (run with BUILD=1)"; echo; echo "RESULT: FAIL"; exit 1; }
fi

# --- 3. Run the container (isolated, ephemeral) ------------------------------
info "Starting container '$NAME' on http://127.0.0.1:${HOST_HTTP_PORT} (ws ${HOST_WS_PORT})"
if ! docker run -d --name "$NAME" \
      -p "127.0.0.1:${HOST_HTTP_PORT}:5000" \
      -p "127.0.0.1:${HOST_WS_PORT}:8765" \
      -v "$ENV_FILE:/app/.env:ro" \
      "$IMAGE" >/dev/null; then
  bad "docker run"; echo; echo "RESULT: FAIL"; exit 1
fi
ok "container started"

# --- 4. Wait for the health endpoint ----------------------------------------
info "Waiting up to ${BOOT_TIMEOUT}s for ${HEALTH_PATH} ..."
base="http://127.0.0.1:${HOST_HTTP_PORT}"
healthy=0
for i in $(seq 1 "$BOOT_TIMEOUT"); do
  # container must still be running
  if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    bad "container exited during boot"; docker logs --tail 40 "$NAME" 2>&1 | sed 's/^/    /'; break
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' "${base}${HEALTH_PATH}" 2>/dev/null || echo 000)"
  if [ "$code" = "200" ]; then healthy=1; ok "health endpoint ${HEALTH_PATH} -> 200 (after ${i}s)"; break; fi
  sleep 1
done
[ "$healthy" = "1" ] || bad "health endpoint did not return 200 within ${BOOT_TIMEOUT}s"

# --- 5. Extra endpoint checks ------------------------------------------------
if [ "$healthy" = "1" ]; then
  root_code="$(curl -s -o /dev/null -w '%{http_code}' "${base}/" 2>/dev/null || echo 000)"
  case "$root_code" in 200|302|301) ok "GET / -> ${root_code}";; *) bad "GET / -> ${root_code}";; esac
fi

# --- 6. Scan boot logs for regressions ---------------------------------------
info "Scanning boot logs for ERROR / traceback"
logs="$(docker logs "$NAME" 2>&1)"
err_count="$(printf '%s\n' "$logs" | grep -icE 'Traceback \(most recent call last\)|CRITICAL|no such table' || true)"
if [ "${err_count:-0}" -eq 0 ]; then ok "no traceback / CRITICAL / 'no such table' in boot logs";
else bad "found ${err_count} traceback/CRITICAL/'no such table' line(s) in boot logs"; printf '%s\n' "$logs" | grep -iE 'Traceback \(most recent call last\)|CRITICAL|no such table' | head -10 | sed 's/^/    /'; fi

# --- 7. Result ---------------------------------------------------------------
echo
echo "----------------------------------------------------------------------"
echo "RESULT: $([ "$FAIL" -eq 0 ] && echo PASS || echo FAIL)   (${PASS} passed, ${FAIL} failed)"
echo "Image: ${IMAGE}   Container: ${NAME}   URL: ${base}"
echo "----------------------------------------------------------------------"
[ "$FAIL" -eq 0 ]
