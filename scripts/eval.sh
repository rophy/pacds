#!/usr/bin/env bash
# Real-LLM evaluations against the Compose dev stack (compose.yaml). Costs LLM usage.
# The LLM comes from LLM_* in the environment, overridden by .env when it exists (see .env.example).
#
# Starts the stack with that LLM, seeds the logs, runs the selected evaluations, then removes the stack.
# Every run gets a directory, eval-runs/<UTC time>/ (or $EVAL_RUN_DIR): run.json (commit, model, arguments),
# eval.log (console output), each evaluation's results, and compose.log (the stack's logs, saved before
# teardown) so every failed request can be traced by its request id, and traces/: every model and tool call, one file
# per PACDS request (traces/pacds/<request id>.json) and per client ticket (traces/<results name>/<case>-<repeat>.json).
# On exit the run's report is written to report/ (python -m tests.analysis report); with PACDS_EVAL_ARCHIVE_S3_URI
# set, the finished run is then uploaded there (python -m tests.eval_run archive), and during the run its new files are
# uploaded every EVAL_SYNC_SECONDS (default 300), so a run lost with its container keeps what it wrote.
# The no-cost functional tests are in scripts/e2e.sh.
#
#   ./scripts/eval.sh --audit                                  exfiltration audit (pass/fail)
#   ./scripts/eval.sh --replay "--set hard --repeat 3"         replay harness with these args (accuracy report)
#   ./scripts/eval.sh --support "--variant full" --support "--variant no-pacds"   support agent (repeatable)
#   --replay-from RUN[,RUN...]   answer every model request identical to one recorded in those runs with the recorded
#             response (PACDS, support agent, baseline); only changed work calls the LLM. Not with --reuse.
#   --reuse   run against a running real-LLM stack; never removes it
#   --keep    keep the stack even when everything passes
#   --target URL   evaluate an already-deployed PACDS (e.g. a corporate evaluation instance) instead of the Compose
#             stack: tokens from TokenSource (tests/oidc.py: PACDS_TOKEN or PACDS_OIDC_*), logs seeded to the store
#             in PACDS_LOGS_S3_* (tests/s3.py; --no-seed to skip), PACDS traces copied from PACDS_TRACE_SOURCE_DIR
#             (the target's trace directory) when set. The client-side LLM (LLM_*) is needed only for --support,
#             baseline replays and the audit. docs/evaluation-runbook.md.
#   --cases-dir DIR   the case set for every step (seeding and all runners): exports PACDS_CASES_DIR
#   --pacds-config FILE   with --target: the target's configuration (python -m pacds.devtools.show_config on its host),
#             recorded in run.json so the report names the PACDS model; without it only the client side is recorded
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
. "$ROOT_DIR/scripts/stack.sh"

REUSE=false
KEEP=false
AUDIT=false
REPLAY_ARGS=()
REPLAY_FROM=""
TARGET=""
SEED=true
PACDS_CONFIG_FILE=""
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
    --target) TARGET="$2"; shift ;;
    --no-seed) SEED=false ;;
    --cases-dir) export PACDS_CASES_DIR="$2"; shift ;;
    --pacds-config) PACDS_CONFIG_FILE="$2"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [ "$AUDIT" = false ] && [ ${#REPLAY_ARGS[@]} -eq 0 ] && [ ${#SUPPORT_ARGS[@]} -eq 0 ]; then
  echo "ERROR: choose at least one of --audit, --replay, --support." >&2
  exit 2
fi

# PACDS, the replay baseline and the support agent all use this LLM. With --target, PACDS has its own, and the
# LLM here is needed only by the clients that call one.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
NEEDS_LLM=true
if [ -n "$TARGET" ]; then
  if [ "$REUSE" = true ] || [ "$KEEP" = true ]; then
    echo "ERROR: --reuse and --keep are about the Compose stack; --target does not start one." >&2
    exit 2
  fi
  case " ${REPLAY_ARGS[*]+${REPLAY_ARGS[*]}} " in *" --baseline "*) ;; *) [ ${#SUPPORT_ARGS[@]} -eq 0 ] && [ "$AUDIT" = false ] && NEEDS_LLM=false ;; esac
fi
if [ "$NEEDS_LLM" = true ] && { [ -z "${LLM_MODEL:-}" ] || [ "$LLM_MODEL" = fake ]; }; then
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

if [ -n "$REPLAY_FROM" ] && [ -n "$TARGET" ]; then
  echo "NOTE: --replay-from with --target replays the clients' calls only; PACDS replays only if the target was started"
  echo "      with those recordings (trace.replay_from)."
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
if [ -n "$REPLAY_FROM" ] && [ -z "$TARGET" ]; then
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
uv run python -m tests.eval_run record "$RUN_DIR" -- ${ARGS[@]+"${ARGS[@]}"}
echo "=== run directory: $RUN_DIR"
if [ "$REUSE" = true ]; then
  echo "NOTE: --reuse: PACDS traces go to the directory of the run that started the stack, if it traced at all."
fi

# Results and client traces go to the run directory unless the caller passed --out / --trace-dir.
with_out() {
  local args="$1"
  case " $args " in *" --out "*) ;; *) args="$args --out $RUN_DIR/$2.json" ;; esac
  case " $args " in *" --trace-dir "*) ;; *) args="$args --trace-dir $RUN_DIR/traces/$2" ;; esac
  if [ -n "$REPLAY_FROM" ]; then args="$args --replay-from $REPLAY_FROM"; fi
  echo "$args"
}

SYNC_PID=""

# Runs on every exit, pass or fail, after the stack logs are saved.
stack_on_exit() {
  if [ -n "$SYNC_PID" ]; then kill "$SYNC_PID" 2>/dev/null || true; fi
  if [ -n "$REPLAY_SOURCE" ]; then rm -rf "$REPLAY_SOURCE"; fi
  if [ -n "$TARGET" ] && [ -n "${PACDS_TRACE_SOURCE_DIR:-}" ]; then
    uv run python -m tests.eval_run collect-traces "$RUN_DIR" "$PACDS_TRACE_SOURCE_DIR" || echo "WARNING: collecting PACDS traces failed"
  fi
  uv run python -m tests.eval_run errors "$RUN_DIR"
  uv run python -m tests.eval_run finish "$RUN_DIR"
  uv run python -m tests.analysis report "$RUN_DIR" >/dev/null && echo "=== report: $RUN_DIR/report/report.md" \
    || echo "WARNING: the report failed; rerun python -m tests.analysis report $RUN_DIR"
  if [ -n "${PACDS_EVAL_ARCHIVE_S3_URI:-}" ]; then
    uv run python -m tests.eval_run archive "$RUN_DIR" || echo "WARNING: archiving the run failed; it is only in $RUN_DIR"
  fi
}

if [ -n "$TARGET" ]; then
  export PACDS_URL="$TARGET"
  trap 'status=$?; stack_on_exit "$status" || true; exit "$status"' EXIT
  echo "=== target: $PACDS_URL ($(curl -fsS "$PACDS_URL/healthz" 2>&1 || echo 'health check failed'))"
  if [ "$SEED" = true ]; then
    if [ -z "${PACDS_CASES_DIR:-}" ]; then echo "=== no --cases-dir / PACDS_CASES_DIR: seeding the repository's cases"; fi
    uv run python -m tests.s3 seed
  fi
  if [ -z "$PACDS_CONFIG_FILE" ]; then echo "WARNING: no --pacds-config; run.json will not record the target's model"; fi
  uv run python -m tests.eval_run manifest "$RUN_DIR" ${PACDS_CONFIG_FILE:+"$PACDS_CONFIG_FILE"}
else
  stack_start
  docker compose exec -T pacds python -m pacds.devtools.show_config >"$RUN_DIR/pacds-config.json" || true
  uv run python -m tests.eval_run manifest "$RUN_DIR" "$RUN_DIR/pacds-config.json"
fi
# Started after stack_start set the exit trap, which stops it.
if [ -n "${PACDS_EVAL_ARCHIVE_S3_URI:-}" ]; then
  (while sleep "${EVAL_SYNC_SECONDS:-300}"; do
     uv run python -m tests.eval_run sync "$RUN_DIR" >/dev/null 2>&1 || echo "WARNING: syncing the run to S3 failed"
   done) &
  SYNC_PID=$!
fi

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
