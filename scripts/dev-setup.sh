#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="pacds"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== PACDS dev setup: LLM config + images (cluster kind-${CLUSTER_NAME}) ==="

# Check prerequisites
for cmd in kind kubectl skaffold docker; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: $cmd is not installed. Please install it first."
    exit 1
  fi
done

# The cluster is created once, outside this script
if ! kubectl config get-contexts -o name 2>/dev/null | grep -qx "kind-${CLUSTER_NAME}"; then
  echo "ERROR: cluster kind-${CLUSTER_NAME} not found. Create it once with: ./scripts/create-cluster.sh"
  exit 1
fi

# Preload the dev S3 image so the cluster does not depend on registry pulls
S3_IMAGE="ghcr.io/rophy/minio:20260423-9db4f623"
docker image inspect "$S3_IMAGE" >/dev/null 2>&1 || docker pull "$S3_IMAGE"
kind load docker-image "$S3_IMAGE" --name "$CLUSTER_NAME"

# LLM config (from .env or the in-cluster fake LLM)
ENV_FILE="$ROOT_DIR/.env"
LLM_BASE_URL="http://pacds-llm:8000/v1"
LLM_MODEL="fake"
LLM_API_KEY="not-needed"
LLM_SESSION_HEADER=""
LLM_API=""

read_env() {
  grep "^$1=" "$ENV_FILE" | cut -d= -f2- || true
}

if [ -f "$ENV_FILE" ]; then
  echo "Found .env file, applying LLM config overrides..."
  LLM_BASE_URL=$(read_env LLM_BASE_URL); LLM_BASE_URL=${LLM_BASE_URL:-http://pacds-llm:8000/v1}
  LLM_MODEL=$(read_env LLM_MODEL); LLM_MODEL=${LLM_MODEL:-fake}
  LLM_API_KEY=$(read_env LLM_API_KEY); LLM_API_KEY=${LLM_API_KEY:-not-needed}
  LLM_SESSION_HEADER=$(read_env LLM_SESSION_HEADER)
  LLM_API=$(read_env LLM_API)
else
  echo "No .env file found. Using the in-cluster fake LLM."
fi

kubectl --context "kind-${CLUSTER_NAME}" apply -f "$ROOT_DIR/k8s/namespace.yaml"

kubectl --context "kind-${CLUSTER_NAME}" -n pacds create configmap pacds-llm-config \
  --from-literal="base-url=$LLM_BASE_URL" \
  --from-literal="model=$LLM_MODEL" \
  --from-literal="session-header=$LLM_SESSION_HEADER" \
  --from-literal="api=$LLM_API" \
  --dry-run=client -o yaml | kubectl --context "kind-${CLUSTER_NAME}" apply -f -

kubectl --context "kind-${CLUSTER_NAME}" -n pacds create secret generic pacds-llm \
  --from-literal="api-key=$LLM_API_KEY" \
  --dry-run=client -o yaml | kubectl --context "kind-${CLUSTER_NAME}" apply -f -

echo "LLM: $LLM_MODEL at $LLM_BASE_URL (api: ${LLM_API:-chat_completions})"
echo ""
echo "Start the dev loop:  skaffold dev --kube-context kind-${CLUSTER_NAME}"
echo "Seed test logs:      ./scripts/seed-logs.sh   (after the first deploy)"
echo "PACDS is port-forwarded to http://localhost:3002"
echo "Test token:          kubectl --context kind-${CLUSTER_NAME} -n pacds create token triage-agent --audience pacds"
echo "Run e2e:             ./scripts/e2e.sh --reuse   (against this deployment)"
