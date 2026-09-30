#!/usr/bin/env bash
# Build the release tarballs: pacds-deploy-VERSION.tar.gz and pacds-samples-VERSION.tar.gz.
#   scripts/build-bundles.sh VERSION OUT_DIR
set -euo pipefail
VERSION=${1:?usage: build-bundles.sh VERSION OUT_DIR}
OUT=${2:?usage: build-bundles.sh VERSION OUT_DIR}
cd "$(dirname "$0")/.."
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

d="$stage/pacds-deploy-$VERSION"
mkdir -p "$d/certs" "$d/docs"
cp deploy/compose.yaml deploy/compose.eval.yaml deploy/compose.eval-replay.yaml deploy/pacds.example.yaml "$d/"
sed "s|^PACDS_IMAGE=.*|PACDS_IMAGE=ghcr.io/rophy/pacds:$VERSION|" deploy/.env.example > "$d/.env.example"
grep -q "^PACDS_IMAGE=ghcr.io/rophy/pacds:$VERSION\$" "$d/.env.example" || { echo "PACDS_IMAGE not stamped" >&2; exit 1; }
cat > "$d/certs/README.md" <<'README'
# certs

Put PEM CA certificate files here (for an internal CA in front of the LLM, git server or log storage).
The directory is mounted read-only at `/etc/pacds/certs`; point `PACDS_CA_FILE` in `.env` at
`/etc/pacds/certs/<file>.pem`.
README
cp docs/deployment.md docs/evaluation-runbook.md "$d/docs/"
tar -C "$stage" -czf "$OUT/pacds-deploy-$VERSION.tar.gz" "pacds-deploy-$VERSION"

s="$stage/pacds-samples-$VERSION"
mkdir -p "$s"
cp -R samples/support-agent "$s/support-agent"
rm -rf "$s/support-agent/__pycache__"
# skills is a symlink to src/pacds_eval/skills: dereference it into real files
rm -rf "$s/support-agent/skills"
cp -RL samples/support-agent/skills "$s/support-agent/skills"
tar -C "$stage" -czf "$OUT/pacds-samples-$VERSION.tar.gz" "pacds-samples-$VERSION"
find "$stage" -type l | grep -q . && { echo "symlinks in the samples bundle" >&2; exit 1; }
ls -l "$OUT/pacds-deploy-$VERSION.tar.gz" "$OUT/pacds-samples-$VERSION.tar.gz"
