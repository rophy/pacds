#!/usr/bin/env bash
# One-time: create the kind-pacds cluster (Calico CNI for NetworkPolicy). Tests and dev setup assume it exists.
set -euo pipefail

CLUSTER_NAME="pacds"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Optional host-local registry pull-through cache (containerd hosts.toml files), mounted on every node.
MIRROR_DIR="${PACDS_KIND_MIRROR_DIR:-$HOME/infra/registry/kind/certs.d}"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "Kind cluster '$CLUSTER_NAME' already exists."
  exit 0
fi

CONFIG="$ROOT_DIR/kind-config.yaml"
if [ -d "$MIRROR_DIR" ]; then
  echo "Using registry mirror config from $MIRROR_DIR"
  CONFIG="$(mktemp)"
  trap 'rm -f "$CONFIG"' EXIT
  python3 - "$ROOT_DIR/kind-config.yaml" "$MIRROR_DIR" >"$CONFIG" <<'PY'
import sys, yaml
config = yaml.safe_load(open(sys.argv[1]))
config.setdefault("containerdConfigPatches", []).append(
    '[plugins."io.containerd.grpc.v1.cri".registry]\n  config_path = "/etc/containerd/certs.d"')
for node in config["nodes"]:
    node.setdefault("extraMounts", []).append({"hostPath": sys.argv[2], "containerPath": "/etc/containerd/certs.d", "readOnly": True})
print(yaml.safe_dump(config, sort_keys=False))
PY
fi

echo "Creating kind cluster '$CLUSTER_NAME'..."
kind create cluster --name "$CLUSTER_NAME" --config "$CONFIG"
echo "Installing Calico CNI for NetworkPolicy support..."
kubectl --context "kind-$CLUSTER_NAME" apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.27.0/manifests/calico.yaml
kubectl --context "kind-$CLUSTER_NAME" -n kube-system rollout status daemonset/calico-node --timeout=180s
echo "Cluster ready. Next: ./scripts/e2e.sh (tests) or ./scripts/dev-setup.sh + skaffold dev (dev loop)."
