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

# Apply LLM config (from .env or defaults)
ENV_FILE="$ROOT_DIR/.env"
LLM_PROVIDER="aimock"
LLM_MODEL="gpt-4o"
LLM_BASE_URL="http://pacds-llm:8000/v1"
LLM_API_KEY="not-needed"

if [ -f "$ENV_FILE" ]; then
  echo ""
  echo "Found .env file, applying LLM config overrides..."
  LLM_PROVIDER=$(grep '^LLM_PROVIDER=' "$ENV_FILE" | cut -d= -f2- || echo "$LLM_PROVIDER")
  LLM_MODEL=$(grep '^LLM_MODEL=' "$ENV_FILE" | cut -d= -f2- || echo "$LLM_MODEL")
  LLM_BASE_URL=$(grep '^LLM_BASE_URL=' "$ENV_FILE" | cut -d= -f2- || echo "$LLM_BASE_URL")
  LLM_API_KEY=$(grep '^LLM_API_KEY=' "$ENV_FILE" | cut -d= -f2- || echo "$LLM_API_KEY")
else
  echo ""
  echo "No .env file found. Using defaults (aimock)."
  echo "To use a real LLM: cp .env.example .env && edit .env"
fi

# Ensure namespace exists first
kubectl --context "kind-${CLUSTER_NAME}" apply -f "$ROOT_DIR/k8s/namespace.yaml"

# Create/update ConfigMap and Secret
kubectl --context "kind-${CLUSTER_NAME}" -n pacds create configmap pacds-llm-config \
  --from-literal="LLM_PROVIDER=$LLM_PROVIDER" \
  --from-literal="LLM_MODEL=$LLM_MODEL" \
  --from-literal="LLM_BASE_URL=$LLM_BASE_URL" \
  --dry-run=client -o yaml | kubectl --context "kind-${CLUSTER_NAME}" apply -f -

kubectl --context "kind-${CLUSTER_NAME}" -n pacds create secret generic pacds-llm \
  --from-literal="api-key=$LLM_API_KEY" \
  --dry-run=client -o yaml | kubectl --context "kind-${CLUSTER_NAME}" apply -f -

echo "LLM config: provider=$LLM_PROVIDER model=$LLM_MODEL"

echo ""
echo "Cluster ready. To start the dev loop:"
echo "  skaffold dev --kube-context kind-${CLUSTER_NAME}"
echo ""
echo "Gateway will be port-forwarded to localhost:3000."
echo ""
echo "To tear down:"
echo "  kind delete cluster --name $CLUSTER_NAME"
