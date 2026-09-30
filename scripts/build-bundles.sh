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
mkdir -p "$s/support-agent"
# only tracked files ship; skills is a symlink to src/pacds_eval/skills, shipped as real files
git ls-files -z -- samples/support-agent ':!samples/support-agent/skills' \
  | tar --null -T - -cf - --transform 's|^samples/support-agent/||' | tar -xf - -C "$s/support-agent"
mkdir -p "$s/support-agent/skills"
git ls-files -z -- src/pacds_eval/skills \
  | tar --null -T - -cf - --transform 's|^src/pacds_eval/skills/||' | tar -xf - -C "$s/support-agent/skills"
links=$(find "$s" -type l) || { echo "find failed" >&2; exit 1; }
[ -z "$links" ] || { echo "symlinks in the samples bundle: $links" >&2; exit 1; }
tar -C "$stage" -czf "$OUT/pacds-samples-$VERSION.tar.gz" "pacds-samples-$VERSION"
ls -l "$OUT/pacds-deploy-$VERSION.tar.gz" "$OUT/pacds-samples-$VERSION.tar.gz"
