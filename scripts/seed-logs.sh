#!/usr/bin/env bash
# Upload the e2e and replay log fixtures (every case set under cases/) to the dev S3 (bucket "logs"). Safe to re-run.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MC=(docker compose --project-directory "$ROOT_DIR" exec -T s3 mc)
BUCKET="logs"

"${MC[@]}" mb --ignore-existing "local/$BUCKET" >/dev/null

upload() {
  "${MC[@]}" pipe --quiet --part-size 5MiB "local/$BUCKET/$2" <"$1" >/dev/null
  echo "  $BUCKET/$2"
}

echo "Seeding s3://$BUCKET"
upload "$ROOT_DIR/tests/e2e/fixtures/checkout.log" "e2e/checkout.log"
for case_dir in "$ROOT_DIR"/cases/*/*/; do
  case_id="$(basename "$case_dir")"
  for log in "$case_dir"*.log; do
    [ -e "$log" ] || continue
    upload "$log" "replay/$case_id/$(basename "$log")"
  done
done
