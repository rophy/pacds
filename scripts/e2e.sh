#!/usr/bin/env bash
# End-to-end tests against the Compose dev stack (compose.yaml).
#
# Starts the stack, seeds the test logs, runs the e2e tests from the host, then removes the stack if everything passed.
#
#   ./scripts/e2e.sh                     full run (the stack must not be running yet)
#   ./scripts/e2e.sh --reuse             run against a running stack; never removes it
#   ./scripts/e2e.sh --keep              keep the stack even when everything passes
#   ./scripts/e2e.sh --replay "--set hard --repeat 3"   also run the replay harness with these args
#   ./scripts/e2e.sh --support "--variant full" --support "--variant no-pacds"   also run the support agent (repeatable)
#
# On any failure the stack is kept for investigation (docker compose down -v before the next full run).
#
# The LLM comes from .env (see .env.example); without .env the fake LLM is used.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REUSE=false
KEEP=false
REPLAY_ARGS=""
SUPPORT_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --reuse) REUSE=true ;;
    --keep) KEEP=true ;;
    --replay) REPLAY_ARGS="$2"; shift ;;
    --support) SUPPORT_ARGS+=("$2"); shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# --- preflight ---------------------------------------------------------------
RUNNING=false
[ -n "$(docker compose ps -q pacds 2>/dev/null)" ] && RUNNING=true
if [ "$REUSE" = false ] && [ "$RUNNING" = true ]; then
  echo "ERROR: the dev stack is already running (a dev session?)." >&2
  echo "Stop it first (docker compose down -v) or run with --reuse." >&2
  exit 1
fi
if [ "$REUSE" = true ] && [ "$RUNNING" = false ]; then
  echo "ERROR: --reuse needs a running stack (docker compose up -d --build --wait)." >&2
  exit 1
fi

# --- teardown (always, unless reusing or keeping) ----------------------------
teardown() {
  local status=$?
  if [ "$REUSE" = true ]; then
    :
  elif [ "$status" -ne 0 ]; then
    # Keep everything for investigation; the next run refuses to start until it is removed.
    echo "=== FAILED (exit $status): keeping the stack for investigation"
    echo "    inspect:  docker compose ps; docker compose logs pacds"
    echo "    rerun:    ./scripts/e2e.sh --reuse"
    echo "    clean up: docker compose down -v"
  elif [ "$KEEP" = true ]; then
    echo "=== keeping the stack (--keep)"
  else
    echo "=== teardown"
    docker compose down -v >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap teardown EXIT

# --- setup -------------------------------------------------------------------
if [ "$REUSE" = false ]; then
  echo "=== setup: start the stack"
  docker compose up -d --build --wait
fi
echo "LLM: $(docker compose exec -T pacds printenv LLM_MODEL) at $(docker compose exec -T pacds printenv LLM_BASE_URL)"
"$ROOT_DIR/scripts/seed-logs.sh" >/dev/null
echo "=== setup: ready"

# The harness and support agent read LLM_* for their own model calls.
if [ -f .env ]; then set -a; . ./.env; set +a; fi

# --- tests -------------------------------------------------------------------
echo "=== e2e tests"
uv run pytest -p no:cacheprovider -m e2e -q
if [ -n "$REPLAY_ARGS" ]; then
  echo "=== replay: $REPLAY_ARGS"
  # shellcheck disable=SC2086 # word splitting of the harness args is intended
  uv run python -m tests.replay.harness $REPLAY_ARGS
fi
for args in ${SUPPORT_ARGS[@]+"${SUPPORT_ARGS[@]}"}; do
  echo "=== support agent: $args"
  # shellcheck disable=SC2086 # word splitting of the runner args is intended
  uv run python -m tests.support_agent.run $args
done
