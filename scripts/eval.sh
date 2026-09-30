#!/usr/bin/env bash
# Real-LLM evaluations against the Compose dev stack (compose.yaml). Costs LLM usage.
# The LLM comes from LLM_* in the environment, overridden by .env when it exists (see .env.example).
#
# Starts the stack with that LLM, seeds the logs, runs the selected evaluations, then removes the stack.
# Every run gets a directory, eval-runs/<UTC time>/ (or $EVAL_RUN_DIR): run.json (commit, model, arguments),
# eval.log (console output), each evaluation's results, and compose.log (the stack's logs, saved before
# teardown) so every failed request can be traced by its request id, and traces/: every model and tool call, one file
# per PACDS request (traces/pacds/<request id>.json) and per client ticket (traces/<results name>/<case>-<repeat>.json).
# The evaluation steps are `pacds eval run` (pacds_eval/run.py) against the stack; this script owns the stack, the LLM
# checks and --audit. On exit the run's report is written to report/ (pacds eval report); with
# PACDS_EVAL_ARCHIVE_S3_URI set, the finished run is then uploaded there (pacds eval runs archive), and during the run
# its new files are uploaded every EVAL_SYNC_SECONDS (default 300), so a run lost with its container keeps what it wrote.
# The no-cost functional tests are in scripts/e2e.sh.
#
#   ./scripts/eval.sh --audit                                  exfiltration audit (pass/fail)
#   ./scripts/eval.sh --replay "--set hard --repeat 3"         replay harness with these args (accuracy report)
#   ./scripts/eval.sh --support "--variant full" --support "--variant no-pacds"   support agent (repeatable)
#   --replay-from RUN[,RUN...]   answer every model request identical to one recorded in those runs with the recorded
#             response (PACDS, support agent, baseline); only changed work calls the LLM. Not with --reuse.
#   --reuse   run against a running real-LLM stack; never removes it
#   --keep    keep the stack even when everything passes
#   --cases-dir DIR   the case set for every step (seeding and all runners): exports PACDS_CASES_DIR
#
# To evaluate an already-deployed PACDS (e.g. a corporate evaluation instance) use `pacds eval run --target URL`
# directly (docs/evaluation-runbook.md).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
. "$ROOT_DIR/scripts/stack.sh"

REUSE=false
KEEP=false
AUDIT=false
REPLAY_ARGS=()
REPLAY_FROM=""
SUPPORT_ARGS=()
ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --reuse) REUSE=true ;;
    --keep) KEEP=true ;;
    --audit) AUDIT=true ;;
    --replay) REPLAY_ARGS+=("$2"); shift ;;
    --support) SUPPORT_ARGS+=("$2"); shift ;;
    --replay-from) REPLAY_FROM="$2"; shift ;;
    --cases-dir) export PACDS_CASES_DIR="$2"; shift ;;
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
export PACDS_CASES_DIR="${PACDS_CASES_DIR:-$ROOT_DIR/cases/github}"  # dev default; pacds_eval has none
if [ -z "${LLM_MODEL:-}" ] || [ "$LLM_MODEL" = fake ]; then
  echo "ERROR: eval needs a real LLM: set LLM_* in .env (see .env.example) or the environment." >&2
  echo "For no-cost tests use scripts/e2e.sh." >&2
  exit 1
fi
# LLM_API=claude_code: the clients run the host's claude; PACDS in the Compose stack needs the CLI in its image and a
# subscription token (claude setup-token).
if [ "${LLM_API:-}" = claude_code ]; then
  command -v claude >/dev/null || { echo "ERROR: LLM_API=claude_code needs the claude CLI on this host." >&2; exit 1; }
  if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "ERROR: LLM_API=claude_code needs CLAUDE_CODE_OAUTH_TOKEN for PACDS in the stack (run: claude setup-token)." >&2
    exit 1
  fi
  export CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-$(claude --version | awk '{print $1}')}"
fi
# As in compose.yaml: a proxy may inject the key, but the harness and support agent need a value.
export LLM_API_KEY="${LLM_API_KEY:-not-needed}"
if [ "$REUSE" = true ] && stack_running && [ "$(stack_llm_model)" != "$LLM_MODEL" ]; then
  echo "ERROR: the running stack uses $(stack_llm_model), but LLM_MODEL is $LLM_MODEL." >&2
  echo "Restart it (docker compose down -v; ./scripts/eval.sh ...)." >&2
  exit 1
fi

if [ -n "$REPLAY_FROM" ] && [ "$REUSE" = true ]; then
  echo "ERROR: --replay-from needs a fresh stack (PACDS loads recordings at start); drop --reuse." >&2
  exit 2
fi

RUN_DIR="${EVAL_RUN_DIR:-$ROOT_DIR/eval-runs/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$RUN_DIR/traces/pacds"
# PACDS runs as uid 10001 and writes its traces into this bind-mounted directory.
chmod 0777 "$RUN_DIR/traces/pacds"
export STACK_LOG_DIR="$RUN_DIR"
export PACDS_TRACE_HOST_DIR="$RUN_DIR/traces/pacds"
exec > >(tee -a "$RUN_DIR/eval.log") 2>&1
REPLAY_SOURCE=""
if [ -n "$REPLAY_FROM" ]; then
  # PACDS gets one read-only directory with every recorded PACDS trace; runners read the client traces directly.
  REPLAY_SOURCE="$ROOT_DIR/eval-runs/.replay/$(basename "$RUN_DIR")"
  mkdir -p "$REPLAY_SOURCE"
  runs=""
  IFS=, read -ra replay_runs <<<"$REPLAY_FROM"
  for run in "${replay_runs[@]}"; do
    run="$(cd "$run" && pwd)"
    runs="${runs:+$runs,}$run"
    [ -d "$run/traces/pacds" ] && cp "$run/traces/pacds/"*.json "$REPLAY_SOURCE/" 2>/dev/null || true
  done
  REPLAY_FROM="$runs"
  chmod -R a+rX "$REPLAY_SOURCE"
  export PACDS_REPLAY_HOST_DIR="$REPLAY_SOURCE"
  echo "=== replaying from $REPLAY_FROM ($(ls "$REPLAY_SOURCE" | wc -l) PACDS traces)"
fi
uv run python -m pacds_eval.runs record "$RUN_DIR" -- ${ARGS[@]+"${ARGS[@]}"}
echo "=== run directory: $RUN_DIR"
if [ "$REUSE" = true ]; then
  echo "NOTE: --reuse: PACDS traces go to the directory of the run that started the stack, if it traced at all."
fi

# Runs on every exit, pass or fail, after the stack logs are saved.
stack_on_exit() {
  if [ -n "$REPLAY_SOURCE" ]; then rm -rf "$REPLAY_SOURCE"; fi
  # pacds eval run --stack leaves this to us: the report and the archive must include compose.log.
  uv run pacds eval runs finalize "$RUN_DIR"
}

stack_start
docker compose exec -T pacds python -m pacds.devtools.show_config >"$RUN_DIR/pacds-config.json" || true

if [ "$AUDIT" = true ]; then
  echo "=== exfiltration audit"
  uv run pytest -p no:cacheprovider -m llm -q --junitxml="$RUN_DIR/audit.xml"
fi
# The evaluation itself (manifest, sync, the steps; eval.sh recorded the run above) is `pacds eval run`; the stack is seeded by seed-logs.sh.
RUN_ARGS=(--target http://localhost:3002 --run-dir "$RUN_DIR" --pacds-config "$RUN_DIR/pacds-config.json" --no-seed --stack)
[ -n "$REPLAY_FROM" ] && RUN_ARGS+=(--replay-from "$REPLAY_FROM")
for args in ${REPLAY_ARGS[@]+"${REPLAY_ARGS[@]}"}; do RUN_ARGS+=(--replay "$args"); done
for args in ${SUPPORT_ARGS[@]+"${SUPPORT_ARGS[@]}"}; do RUN_ARGS+=(--support "$args"); done
uv run pacds eval run "${RUN_ARGS[@]}"
