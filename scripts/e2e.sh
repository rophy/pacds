#!/usr/bin/env bash
# End-to-end tests inside the existing kind-pacds cluster.
#
# Creates the "pacds" namespace, deploys PACDS and its dev backends (skaffold), seeds the test logs,
# runs the tests from the in-cluster test runner pod, then deletes the namespace if everything passed.
#
#   ./scripts/e2e.sh                     full run (namespace must not exist yet)
#   ./scripts/e2e.sh --reuse             run against an existing deployment; never deletes the namespace
#   ./scripts/e2e.sh --keep              keep the namespace even when everything passes
#   ./scripts/e2e.sh --replay "--set hard --repeat 3"   also run the replay harness with these args
#
# On any failure the namespace is kept for investigation (delete it before the next full run).
#
# The LLM comes from .env (see scripts/dev-setup.sh); without .env the in-cluster fake LLM is used.
set -euo pipefail

CONTEXT="kind-pacds"
NAMESPACE="pacds"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBECTL=(kubectl --context "$CONTEXT" -n "$NAMESPACE")

REUSE=false
KEEP=false
REPLAY_ARGS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --reuse) REUSE=true ;;
    --keep) KEEP=true ;;
    --replay) REPLAY_ARGS="$2"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# --- preflight ---------------------------------------------------------------
if ! kubectl config get-contexts -o name 2>/dev/null | grep -qx "$CONTEXT"; then
  echo "ERROR: cluster $CONTEXT not found. Create it once with: ./scripts/create-cluster.sh" >&2
  exit 1
fi
NAMESPACE_EXISTS=false
kubectl --context "$CONTEXT" get namespace "$NAMESPACE" >/dev/null 2>&1 && NAMESPACE_EXISTS=true
if [ "$REUSE" = false ] && [ "$NAMESPACE_EXISTS" = true ]; then
  echo "ERROR: namespace $NAMESPACE already exists (a dev deployment?)." >&2
  echo "Delete it first (kubectl --context $CONTEXT delete namespace $NAMESPACE) or run with --reuse." >&2
  exit 1
fi
if [ "$REUSE" = true ] && [ "$NAMESPACE_EXISTS" = false ]; then
  echo "ERROR: --reuse needs an existing namespace $NAMESPACE." >&2
  exit 1
fi

# --- teardown (always, unless reusing or keeping) ----------------------------
teardown() {
  local status=$?
  if [ "$REUSE" = true ]; then
    :
  elif [ "$status" -ne 0 ]; then
    # Keep everything for investigation; the next run refuses to start until it is deleted.
    echo "=== FAILED (exit $status): keeping namespace $NAMESPACE for investigation"
    echo "    inspect:  kubectl --context $CONTEXT -n $NAMESPACE get pods; ... logs deploy/pacds"
    echo "    rerun:    ./scripts/e2e.sh --reuse"
    echo "    clean up: kubectl --context $CONTEXT delete namespace $NAMESPACE"
  elif [ "$KEEP" = true ]; then
    echo "=== keeping namespace $NAMESPACE (--keep)"
  else
    echo "=== teardown: deleting namespace $NAMESPACE"
    kubectl --context "$CONTEXT" delete namespace "$NAMESPACE" --wait=false >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap teardown EXIT

# --- setup -------------------------------------------------------------------
if [ "$REUSE" = false ]; then
  echo "=== setup: namespace $NAMESPACE"
  kubectl --context "$CONTEXT" apply -f "$ROOT_DIR/k8s/namespace.yaml"
fi
"$ROOT_DIR/scripts/dev-setup.sh" | grep -E "^(LLM|ERROR)"
echo "=== setup: deploy"
skaffold run --kube-context "$CONTEXT" >/dev/null
for deploy in pacds pacds-s3 pacds-test-runner; do
  "${KUBECTL[@]}" rollout status "deploy/$deploy" --timeout=180s >/dev/null
done
"$ROOT_DIR/scripts/seed-logs.sh" >/dev/null 2>&1
echo "=== setup: ready"

# --- tests -------------------------------------------------------------------
echo "=== e2e tests (in-cluster)"
"${KUBECTL[@]}" exec deploy/pacds-test-runner -- pytest -p no:cacheprovider -m e2e -q
if [ -n "$REPLAY_ARGS" ]; then
  echo "=== replay: $REPLAY_ARGS"
  # shellcheck disable=SC2086 # word splitting of the harness args is intended
  "${KUBECTL[@]}" exec deploy/pacds-test-runner -- python -m tests.replay.harness $REPLAY_ARGS
fi
