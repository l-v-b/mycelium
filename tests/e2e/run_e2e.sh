#!/usr/bin/env bash
# Hermetic end-to-end run: postgres+pgvector + the mycelium server, then runs
# the black-box + white-box contract suite against it.
#
# Both deployment modes share the same synchronous pgvector write path, so a
# single stack is representative (the old personal-vs-team / Redis-worker /
# parity split went away with the ChromaDB->pgvector migration).
#
# Anything non-zero fails the run. Set KEEP=1 to leave the stack up for
# debugging; otherwise it's torn down (including volumes) on exit.
#
# Usage:  tests/e2e/run_e2e.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

COMPOSE="docker compose -f docker-compose.e2e.yml"
VENV="$REPO_ROOT/.e2e-venv"
URL="http://localhost:9101/sse"

cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then
    echo "==> tearing down e2e stack"
    $COMPOSE down -v --remove-orphans || true
  else
    echo "==> KEEP=1 set; leaving stack up ($COMPOSE down -v to clean)"
  fi
}
trap cleanup EXIT

wait_tcp() {  # host port name
  local host="$1" port="$2" name="$3" i
  echo -n "==> waiting for $name ($host:$port) "
  for i in $(seq 1 90); do
    if python3 -c "import socket; socket.create_connection(('$host', $port), 2).close()" 2>/dev/null; then
      echo "ok"; return 0
    fi
    echo -n "."; sleep 2
  done
  echo "TIMEOUT"; return 1
}

echo "==> building + starting stack (postgres+pgvector + mycelium server)"
$COMPOSE up -d --build

wait_tcp localhost 9101 mycelium

echo "==> setting up test venv"
[[ -d "$VENV" ]] || python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install -q --upgrade pip
pip install -q -r tests/e2e/requirements.txt

PYTEST="python -m pytest -c tests/e2e/pytest.ini tests/e2e"
rc=0

echo
echo "############ contract + white-box (pgvector stack) ############"
# Single synchronous stack: exclude the retired team-async + parity markers.
# The white-box disk/provenance probes need the server container name.
MYCELIUM_E2E_URL="$URL" \
MYCELIUM_E2E_MODE=personal \
MYCELIUM_E2E_SERVER_CONTAINER=mycelium-e2e-server \
MYCELIUM_E2E_ALLOW_DIARY=1 \
  $PYTEST -m "not parity and not team_only" || rc=$?

echo
if [[ $rc -eq 0 ]]; then
  echo "==> E2E PASSED"
else
  echo "==> E2E FAILED (rc=$rc)"
fi
exit $rc
