#!/usr/bin/env bash
#
# Runs the server suite with a timeout that fits it, and a log to read
# afterwards.
#
# The full suite is ~120-160s and needs a Postgres container, so it was
# repeatedly started with a guessed timeout that was shorter than the run. A
# killed pytest does not reach the fixture's cleanup, so each one also leaked
# its database container. This script exists so the slow path has one spelling:
# a generous timeout, output on disk, and a last line that says which it was.
#
# Usage:
#   scripts/test.sh              # everything (~160s, needs Docker)
#   scripts/test.sh fast         # only the tests that need no database (~20s)
#   scripts/test.sh db           # only the tests that do
#   scripts/test.sh apps/server/tests/test_routing_and_limits.py -k window
#
# Anything that is not `fast` or `db` is passed to pytest as it is. Overridable
# through the environment: ANA_TEST_TIMEOUT (seconds), ANA_TEST_LOG (path).

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TIMEOUT="${ANA_TEST_TIMEOUT:-600}"
LOG="${ANA_TEST_LOG:-${TMPDIR:-/tmp}/ana-pytest.log}"

case "${1-}" in
  fast) shift; set -- -m "not db" "$@" ;;
  db)   shift; set -- -m db "$@" ;;
esac

PYTEST="$ROOT/.venv/bin/pytest"
[ -x "$PYTEST" ] || PYTEST="pytest"

# KILL after TERM: a pytest wedged in a subprocess ignores the first signal,
# and the point of this script is that the run always ends by itself.
timeout --kill-after=30 "$TIMEOUT" "$PYTEST" -q "$@" 2>&1 | tee "$LOG"
status="${PIPESTATUS[0]}"

case "$status" in
  0)   echo "pass  (log: $LOG)" ;;
  124|137) echo "TIMED OUT after ${TIMEOUT}s, so the run was killed: no verdict. The log is $LOG." ;;
  *)   echo "FAIL (exit $status; log: $LOG)" ;;
esac
exit "$status"
