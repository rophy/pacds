# Compose dev stack lifecycle shared by e2e.sh and eval.sh. Source it; do not run it.
#
# The caller sets ROOT_DIR, REUSE and KEEP, then calls stack_start after choosing the LLM (LLM_* in the environment).
# On any failure the stack is kept for investigation (docker compose down -v before the next full run).
# With STACK_LOG_DIR set, the stack's logs since stack_start are saved there before any teardown, pass or fail.
# With PACDS_TRACE_HOST_DIR set, PACDS writes a trace per request there (dev/compose.trace.yaml); with
# PACDS_REPLAY_HOST_DIR set, it replays the recorded model calls in the traces there (dev/compose.replay.yaml).
# A caller-defined stack_on_exit function runs last on exit, with the exit status, pass or fail.

stack_running() {
  [ -n "$(docker compose ps -q pacds 2>/dev/null)" ]
}

stack_llm_model() {
  docker compose exec -T pacds printenv LLM_MODEL
}

# PACDS reports errors to clients tersely by design; its own logs hold the reasons, keyed by request id.
stack_save_logs() {
  [ -n "${STACK_LOG_DIR:-}" ] && stack_running || return 0
  docker compose logs --no-color --timestamps --since "$STACK_STARTED_AT" >"$STACK_LOG_DIR/compose.log" 2>&1 || true
  echo "=== stack logs saved: $STACK_LOG_DIR/compose.log"
}

stack_teardown() {
  local status=$?
  stack_save_logs
  if [ "$REUSE" = true ]; then
    :
  elif [ "$status" -ne 0 ]; then
    # Keep everything for investigation; the next run refuses to start until it is removed.
    echo "=== FAILED (exit $status): keeping the stack for investigation"
    echo "    inspect:  docker compose ps; docker compose logs pacds"
    echo "    rerun:    $0 --reuse ..."
    echo "    clean up: docker compose down -v"
  elif [ "$KEEP" = true ]; then
    echo "=== keeping the stack (--keep)"
  else
    echo "=== teardown"
    docker compose down -v >/dev/null 2>&1 || true
  fi
  if declare -F stack_on_exit >/dev/null; then stack_on_exit "$status" || true; fi
  exit "$status"
}

# Starts the stack (or checks the running one with --reuse) and seeds the log fixtures.
stack_start() {
  local layers=""
  [ -n "${PACDS_TRACE_HOST_DIR:-}" ] && layers="$layers:dev/compose.trace.yaml"
  [ -n "${PACDS_REPLAY_HOST_DIR:-}" ] && layers="$layers:dev/compose.replay.yaml"
  if [ -n "$layers" ]; then
    # Setting COMPOSE_FILE stops Compose from loading compose.override.yaml by itself.
    COMPOSE_FILE="compose.yaml"
    [ -f compose.override.yaml ] && COMPOSE_FILE="$COMPOSE_FILE:compose.override.yaml"
    export COMPOSE_FILE="$COMPOSE_FILE$layers"
  fi
  if [ "$REUSE" = false ] && stack_running; then
    echo "ERROR: the dev stack is already running (a dev session?)." >&2
    echo "Stop it first (docker compose down -v) or run with --reuse." >&2
    exit 1
  fi
  if [ "$REUSE" = true ] && ! stack_running; then
    echo "ERROR: --reuse needs a running stack (docker compose up -d --build --wait)." >&2
    exit 1
  fi
  STACK_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  trap stack_teardown EXIT
  if [ "$REUSE" = false ]; then
    echo "=== setup: start the stack"
    docker compose up -d --build --wait
  fi
  echo "LLM: $(stack_llm_model) at $(docker compose exec -T pacds printenv LLM_BASE_URL)"
  "$ROOT_DIR/scripts/seed-logs.sh" >/dev/null
  echo "=== setup: ready"
}
