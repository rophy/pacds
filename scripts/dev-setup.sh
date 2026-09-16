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

echo ""
echo "Cluster ready. To start the dev loop:"
echo "  skaffold dev --kube-context kind-${CLUSTER_NAME}"
echo ""
echo "Gateway will be port-forwarded to localhost:3000."
echo ""
echo "Test with:"
echo "  curl -X POST http://localhost:3000/api/v1/diagnose \\"
echo "    -H 'Authorization: Bearer dev-token' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"service\": \"checkout-service\", \"timeRange\": {\"start\": \"2026-09-17T00:00:00Z\", \"end\": \"2026-09-17T01:00:00Z\"}}'"
echo ""
echo "To tear down:"
echo "  kind delete cluster --name $CLUSTER_NAME"
