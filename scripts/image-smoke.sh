#!/usr/bin/env bash
# Smoke test of a built PACDS image: entrypoint, subcommands, bundled sample, and a serving container.
#   scripts/image-smoke.sh IMAGE
set -euo pipefail
IMAGE=${1:?usage: image-smoke.sh IMAGE}
cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }

out=$(docker run --rm "$IMAGE") || fail "bare run exited non-zero"
grep -q serve <<<"$out" || fail "bare run does not print the commands"
docker run --rm "$IMAGE" eval --help >/dev/null || fail "eval --help"
docker run --rm "$IMAGE" check-llm --help >/dev/null || fail "check-llm --help"
docker run --rm --entrypoint ls "$IMAGE" /opt/pacds/samples/support-agent/skills/tech-support/SKILL.md >/dev/null \
  || fail "sample skills missing"
links=$(docker run --rm --entrypoint find "$IMAGE" /opt/pacds -type l) || fail "find in /opt/pacds"
[ -z "$links" ] || fail "symlinks in /opt/pacds (the sample must be real files): $links"

name=pacds-smoke-$$
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$name" -v "$PWD/dev/pacds.yaml:/etc/pacds/config.yaml:ro" \
  -e PACDS_CONFIG=/etc/pacds/config.yaml -e LLM_BASE_URL=http://localhost:1/v1 -e LLM_MODEL=fake -e LLM_API_KEY=x \
  -e LLM_SESSION_HEADER= -e LLM_API= -e LLM_MAX_OUTPUT_TOKENS= -e LLM_EFFORT= -e PACDS_CONTEXT_BUDGET= \
  -e PACDS_TRACE_DIR= -e PACDS_REPLAY_DIR= --tmpfs /var/cache/pacds:mode=1777 --tmpfs /tmp \
  --read-only --cap-drop ALL --user 10001 \
  "$IMAGE" serve >/dev/null
health=
for _ in $(seq 30); do
  health=$(docker exec "$name" python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/healthz', timeout=2).read().decode())" 2>/dev/null) && break
  sleep 1
done
grep -q '"version"' <<<"$health" || { docker logs "$name" >&2; fail "/healthz did not answer with a version: $health"; }
echo "image smoke OK: $health"
