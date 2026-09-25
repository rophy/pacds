#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="pacds"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== PACDS Local Dev Setup ==="

# Check prerequisites
for cmd in kind kubectl skaffold docker; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: $cmd is not installed. Please install it first."
    exit 1
  fi
done

# Create Kind cluster if it doesn't exist
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  echo "Kind cluster '$CLUSTER_NAME' already exists, skipping creation."
else
  echo "Creating Kind cluster '$CLUSTER_NAME' with Calico CNI..."
  kind create cluster --name "$CLUSTER_NAME" --config "$ROOT_DIR/kind-config.yaml"

  echo "Installing Calico CNI for NetworkPolicy support..."
  kubectl --context "kind-${CLUSTER_NAME}" apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.27.0/manifests/calico.yaml

  echo "Waiting for Calico to be ready..."
  kubectl --context "kind-${CLUSTER_NAME}" -n kube-system rollout status daemonset/calico-node --timeout=120s
fi

# LLM config (from .env or the in-cluster fake LLM)
ENV_FILE="$ROOT_DIR/.env"
LLM_BASE_URL="http://pacds-llm:8000/v1"
LLM_MODEL="fake"
LLM_API_KEY="not-needed"

read_env() {
  grep "^$1=" "$ENV_FILE" | cut -d= -f2- || true
}

if [ -f "$ENV_FILE" ]; then
  echo "Found .env file, applying LLM config overrides..."
  LLM_BASE_URL=$(read_env LLM_BASE_URL); LLM_BASE_URL=${LLM_BASE_URL:-http://pacds-llm:8000/v1}
  LLM_MODEL=$(read_env LLM_MODEL); LLM_MODEL=${LLM_MODEL:-fake}
  LLM_API_KEY=$(read_env LLM_API_KEY); LLM_API_KEY=${LLM_API_KEY:-not-needed}
else
  echo "No .env file found. Using the in-cluster fake LLM."
fi

kubectl --context "kind-${CLUSTER_NAME}" apply -f "$ROOT_DIR/k8s/namespace.yaml"

kubectl --context "kind-${CLUSTER_NAME}" -n pacds create configmap pacds-llm-config \
  --from-literal="base-url=$LLM_BASE_URL" \
  --from-literal="model=$LLM_MODEL" \
  --dry-run=client -o yaml | kubectl --context "kind-${CLUSTER_NAME}" apply -f -

kubectl --context "kind-${CLUSTER_NAME}" -n pacds create secret generic pacds-llm \
  --from-literal="api-key=$LLM_API_KEY" \
  --dry-run=client -o yaml | kubectl --context "kind-${CLUSTER_NAME}" apply -f -

echo "LLM: $LLM_MODEL at $LLM_BASE_URL"
echo ""
echo "Start the dev loop:  skaffold dev --kube-context kind-${CLUSTER_NAME}"
echo "PACDS is port-forwarded to http://localhost:3002"
echo "Test token:          kubectl --context kind-${CLUSTER_NAME} -n support create token triage-agent --audience pacds"
echo "Tear down:           kind delete cluster --name $CLUSTER_NAME"
