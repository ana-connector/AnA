#!/usr/bin/env bash
#
# Brings up an ANA that the browser checks can drive end to end, and takes it
# down again. Nothing it starts touches the development stack, the real
# database or a CLI account:
#
#   - a throwaway pgvector container, published on 127.0.0.1 only,
#   - the real FastAPI server, with its own state directory and workspace
#     root, and `apps/server/tests/fixtures/fake_claude_cli.py` on PATH as
#     `claude` so turns run without an account or a quota,
#   - the real Next development server, built to reach that API port.
#
# Usage:
#   scripts/e2e-stack.sh up        # start, wait until both answer
#   scripts/e2e-stack.sh down      # stop everything and remove the container
#   scripts/e2e-stack.sh check     # up, run the browser checks, down
#
# Ports and paths are overridable through the environment (E2E_WEB_PORT,
# E2E_API_PORT, E2E_DB_PORT, E2E_DIR).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
E2E_DIR="${E2E_DIR:-${TMPDIR:-/tmp}/ana-e2e}"
E2E_WEB_PORT="${E2E_WEB_PORT:-3211}"
E2E_API_PORT="${E2E_API_PORT:-8123}"
E2E_DB_PORT="${E2E_DB_PORT:-15499}"
CONTAINER="ana-e2e-pg"
# Resolved to a full path: the server is started with `env -i`, which drops
# PATH, so a bare interpreter name would not be found there.
PYTHON="$(command -v "${PYTHON:-$ROOT/.venv/bin/python}" || true)"
if [ -z "$PYTHON" ]; then
  echo "[e2e] no python found (set PYTHON=/path/to/python)" >&2
  exit 1
fi

api_url="http://127.0.0.1:${E2E_API_PORT}"
web_url="http://127.0.0.1:${E2E_WEB_PORT}"

log() { printf '[e2e] %s\n' "$*"; }

port_pid() {
  ss -ltnp 2>/dev/null | grep ":$1 " | grep -oP 'pid=\K[0-9]+' | head -1
}

wait_for() {
  local url="$1" tries="${2:-60}"
  for _ in $(seq 1 "$tries"); do
    if curl -sf -o /dev/null "$url"; then return 0; fi
    sleep 2
  done
  return 1
}

up() {
  mkdir -p "$E2E_DIR/bin" "$E2E_DIR/state" "$E2E_DIR/workspaces"
  # `claude` on PATH is the stand-in, so no account is ever reached.
  cp "$ROOT/apps/server/tests/fixtures/fake_claude_cli.py" "$E2E_DIR/bin/claude"
  chmod +x "$E2E_DIR/bin/claude"

  log "database"
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$CONTAINER" \
    -e POSTGRES_PASSWORD=test -e POSTGRES_DB=ana_e2e \
    -p "127.0.0.1:${E2E_DB_PORT}:5432" pgvector/pgvector:pg16 >/dev/null
  for _ in $(seq 1 60); do
    docker exec "$CONTAINER" pg_isready -U postgres -d ana_e2e >/dev/null 2>&1 && break
    sleep 1
  done

  log "server on ${E2E_API_PORT}"
  # env -i: the server must not inherit this shell's ANA_* settings, a real
  # DATABASE_URL or a gate token, for the same reason the test suite clears
  # them (apps/server/tests/conftest.py).
  setsid env -i HOME="$HOME" PATH="$E2E_DIR/bin:/usr/bin:/bin" \
    DATABASE_URL="postgresql://postgres:test@127.0.0.1:${E2E_DB_PORT}/ana_e2e" \
    ANA_ALLOWED_ORIGINS="${web_url},http://localhost:${E2E_WEB_PORT}" \
    ANA_STATE_DIR="$E2E_DIR/state" \
    ANA_WORKSPACE_ROOT="$E2E_DIR/workspaces" \
    ANA_SECRET_KEY="e2e" \
    "$PYTHON" -u -m uvicorn apps.server.app.main:app \
      --app-dir "$ROOT" --host 127.0.0.1 --port "$E2E_API_PORT" \
    > "$E2E_DIR/server.log" 2>&1 &
  # Generous: a first run downloads the embedding model before the app is
  # ready, which takes longer than anything else here.
  wait_for "${api_url}/workspaces" 150 || {
    echo "[e2e] the server never answered; its log follows:" >&2
    cat "$E2E_DIR/server.log" >&2 || echo "[e2e] (the log is empty: it never started)" >&2
    return 1
  }

  log "web on ${E2E_WEB_PORT}"
  # The port is read at build time, so it is set for this process only.
  (cd "$ROOT/apps/web" && setsid env NEXT_PUBLIC_WS_PORT="$E2E_API_PORT" \
    npm run dev -- -p "$E2E_WEB_PORT" > "$E2E_DIR/web.log" 2>&1 &)
  wait_for "${web_url}/" 90 || {
    echo "[e2e] the web server never answered; its log follows:" >&2
    cat "$E2E_DIR/web.log" >&2 || true
    return 1
  }

  log "ready: ${web_url} against ${api_url}"
}

down() {
  for port in "$E2E_WEB_PORT" "$E2E_API_PORT"; do
    pid="$(port_pid "$port" || true)"
    [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null || true
  done
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  log "down"
}

check() {
  up
  local failed=0
  for script in "$ROOT"/apps/web/tests/browser/test_*.py; do
    log "$(basename "$script")"
    "$PYTHON" "$script" || failed=1
  done
  down
  return "$failed"
}

case "${1:-check}" in
  up) up ;;
  down) down ;;
  check) check ;;
  *) echo "usage: $0 [up|down|check]" >&2; exit 2 ;;
esac
