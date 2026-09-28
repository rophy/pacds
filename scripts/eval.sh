#!/usr/bin/env bash
# Real-LLM evaluations against the Compose dev stack (compose.yaml). Costs LLM usage.
# The LLM comes from LLM_* in the environment, overridden by .env when it exists (see .env.example).
#
# Starts the stack with that LLM, seeds the logs, runs the selected evaluations, then removes the stack.
# Every run gets a directory, eval-runs/<UTC time>/ (or $EVAL_RUN_DIR): run.json (commit, model, arguments),
# eval.log (console output), each evaluation's results, and compose.log (the stack's logs, saved before
# teardown) so every failed request can be traced by its request id. The no-cost functional tests are in scripts/e2e.sh.
#
#   ./scripts/eval.sh --audit                                  exfiltration audit (pass/fail)
#   ./scripts/eval.sh --replay "--set hard --repeat 3"         replay harness with these args (accuracy report)
#   ./scripts/eval.sh --support "--variant full" --support "--variant no-pacds"   support agent (repeatable)
#   --reuse   run against a running real-LLM stack; never removes it
#   --keep    keep the stack even when everything passes
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
. "$ROOT_DIR/scripts/stack.sh"

REUSE=false
KEEP=false
AUDIT=false
REPLAY_ARGS=()
SUPPORT_ARGS=()
ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --reuse) REUSE=true ;;
    --keep) KEEP=true ;;
    --audit) AUDIT=true ;;
    --replay) REPLAY_ARGS+=("$2"); shift ;;
    --support) SUPPORT_ARGS+=("$2"); shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [ "$AUDIT" = false ] && [ ${#REPLAY_ARGS[@]} -eq 0 ] && [ ${#SUPPORT_ARGS[@]} -eq 0 ]; then
  echo "ERROR: choose at least one of --audit, --replay, --support." >&2
  exit 2
fi

# PACDS, the replay baseline and the support agent all use this LLM.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
if [ -z "${LLM_MODEL:-}" ] || [ "$LLM_MODEL" = fake ]; then
  echo "ERROR: eval needs a real LLM: set LLM_* in .env (see .env.example) or the environment." >&2
  echo "For no-cost tests use scripts/e2e.sh." >&2
  exit 1
fi
# As in compose.yaml: a proxy may inject the key, but the harness and support agent need a value.
export LLM_API_KEY="${LLM_API_KEY:-not-needed}"
if [ "$REUSE" = true ] && stack_running && [ "$(stack_llm_model)" != "$LLM_MODEL" ]; then
  echo "ERROR: the running stack uses $(stack_llm_model), but LLM_MODEL is $LLM_MODEL." >&2
  echo "Restart it (docker compose down -v; ./scripts/eval.sh ...)." >&2
  exit 1
fi

RUN_DIR="${EVAL_RUN_DIR:-$ROOT_DIR/eval-runs/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$RUN_DIR"
export STACK_LOG_DIR="$RUN_DIR"
exec > >(tee -a "$RUN_DIR/eval.log") 2>&1
uv run python -m tests.eval_run record "$RUN_DIR" -- ${ARGS[@]+"${ARGS[@]}"}
echo "=== run directory: $RUN_DIR"

# Results go to the run directory unless the caller passed --out.
with_out() { case " $1 " in *" --out "*) echo "$1" ;; *) echo "$1 --out $RUN_DIR/$2.json" ;; esac; }

stack_start

if [ "$AUDIT" = true ]; then
  echo "=== exfiltration audit"
  uv run pytest -p no:cacheprovider -m llm -q --junitxml="$RUN_DIR/audit.xml"
fi
n=0
for args in ${REPLAY_ARGS[@]+"${REPLAY_ARGS[@]}"}; do
  n=$((n + 1)); args="$(with_out "$args" "replay-$n")"
  echo "=== replay: $args"
  # shellcheck disable=SC2086 # word splitting of the harness args is intended
  uv run python -m tests.replay.harness $args
done
n=0
for args in ${SUPPORT_ARGS[@]+"${SUPPORT_ARGS[@]}"}; do
  n=$((n + 1)); args="$(with_out "$args" "support-$n")"
  echo "=== support agent: $args"
  # shellcheck disable=SC2086 # word splitting of the runner args is intended
  uv run python -m tests.support_agent.run $args
done
uv run python -m tests.eval_run errors "$RUN_DIR"
