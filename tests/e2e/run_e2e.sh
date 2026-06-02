#!/usr/bin/env bash
# Hermetic end-to-end run: brings up a personal stack AND a full team stack
# (server + Redis + writer worker), then runs the suite three ways:
#   1. full contract + white-box against the PERSONAL stack
#   2. full contract + white-box + async lifecycle against the TEAM stack
#   3. the parity oracle comparing the two
#
# Anything non-zero fails the run. Set KEEP=1 to leave the stacks up for
# debugging; otherwise they're torn down (including volumes) on exit.
#
# Usage:  tests/e2e/run_e2e.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

COMPOSE="docker compose -f docker-compose.e2e.yml"
VENV="$REPO_ROOT/.e2e-venv"
PERSONAL_URL="http://localhost:9101/sse"
TEAM_URL="http://localhost:9102/sse"

cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then
    echo "==> tearing down e2e stacks"
    $COMPOSE down -v --remove-orphans || true
  else
    echo "==> KEEP=1 set; leaving stacks up ($COMPOSE down -v to clean)"
  fi
}
trap cleanup EXIT

wait_tcp() {  # host port name
  local host="$1" port="$2" name="$3" i
  echo -n "==> waiting for $name ($host:$port) "
  for i in $(seq 1 60); do
    if python3 -c "import socket; socket.create_connection(('$host', $port), 2).close()" 2>/dev/null; then
      echo "ok"; return 0
    fi
    echo -n "."; sleep 2
  done
  echo "TIMEOUT"; return 1
}

echo "==> building + starting stacks"
$COMPOSE up -d --build

wait_tcp localhost 9101 personal
wait_tcp localhost 9102 team-server

echo "==> setting up test venv"
[[ -d "$VENV" ]] || python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install -q --upgrade pip
pip install -q -r tests/e2e/requirements.txt

PYTEST="python -m pytest -c tests/e2e/pytest.ini tests/e2e"
rc=0

echo
echo "############ (1/3) PERSONAL stack — contract + white-box ############"
MYCELIUM_E2E_URL="$PERSONAL_URL" \
MYCELIUM_E2E_MODE=personal \
MYCELIUM_E2E_SERVER_CONTAINER=mycelium-e2e-personal \
MYCELIUM_E2E_ALLOW_DIARY=1 \
  $PYTEST -m "not parity and not team_only" || rc=$?

echo
echo "############ (2/3) TEAM stack — contract + white-box + lifecycle ############"
MYCELIUM_E2E_URL="$TEAM_URL" \
MYCELIUM_E2E_MODE=team \
MYCELIUM_E2E_SERVER_CONTAINER=mycelium-e2e-team-server \
MYCELIUM_E2E_WORKER_CONTAINER=mycelium-e2e-team-worker \
MYCELIUM_E2E_REDIS_CONTAINER=mycelium-e2e-redis \
MYCELIUM_E2E_COMMIT_TIMEOUT=90 \
MYCELIUM_E2E_ALLOW_DIARY=1 \
  $PYTEST -m "not parity" || rc=$?

echo
echo "############ (3/3) PARITY oracle — personal vs team ############"
MYCELIUM_E2E_URL_PERSONAL="$PERSONAL_URL" \
MYCELIUM_E2E_URL_TEAM="$TEAM_URL" \
MYCELIUM_E2E_COMMIT_TIMEOUT=90 \
  $PYTEST -m parity || rc=$?

echo
if [[ $rc -eq 0 ]]; then
  echo "==> E2E PASSED"
else
  echo "==> E2E FAILED (rc=$rc)"
fi
exit $rc
