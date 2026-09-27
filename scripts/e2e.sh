#!/usr/bin/env bash
# Functional e2e tests against the Compose dev stack (compose.yaml), always with the fake LLM: no LLM cost.
#
# Starts the stack, seeds the test logs, runs the e2e tests from the host, then removes the stack if everything passed.
# Real-LLM runs (exfiltration audit, replay, support agent) are in scripts/eval.sh.
#
#   ./scripts/e2e.sh                     full run (the stack must not be running yet)
#   ./scripts/e2e.sh --reuse             run against a running fake-LLM stack; never removes it
#   ./scripts/e2e.sh --keep              keep the stack even when everything passes
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
. "$ROOT_DIR/scripts/stack.sh"

REUSE=false
KEEP=false
while [ $# -gt 0 ]; do
  case "$1" in
    --reuse) REUSE=true ;;
    --keep) KEEP=true ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# The shell environment wins over .env in Compose, so the stack gets the fake LLM even when .env exists.
export LLM_BASE_URL=http://fake-llm:8000/v1 LLM_MODEL=fake LLM_API_KEY=not-needed LLM_SESSION_HEADER= LLM_API=
if [ "$REUSE" = true ] && stack_running && [ "$(stack_llm_model)" != fake ]; then
  echo "ERROR: the running stack uses a real LLM ($(stack_llm_model)); e2e needs the fake one." >&2
  echo "Restart it (docker compose down -v; ./scripts/e2e.sh) or use scripts/eval.sh." >&2
  exit 1
fi

stack_start

echo "=== e2e tests"
uv run pytest -p no:cacheprovider -m e2e -q
