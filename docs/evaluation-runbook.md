# Evaluation runbook

How to measure PACDS in a corporate environment: an evaluation instance of PACDS next to production, case sets of
real tickets, and the harness in this repository driving them. Design: docs/superpowers/specs/2026-09-28-eval-analysis-framework-design.md.

## 1. Pieces

| Piece | Where | Role |
|---|---|---|
| Evaluation PACDS | `deploy/compose.yaml` + `deploy/compose.eval.yaml` on the VM | the service under test, writing a trace per request |
| Log store | S3-compatible (MinIO/Ceph) | holds the cases' log files; the harness hands PACDS presigned URLs |
| Harness | this repository, checked out on a machine that reaches PACDS, the store and the LLM | sends requests, runs the support agent and baseline, analyses |
| Client LLM | vLLM (`LLM_*`) | the support agent, the no-code baseline and the failure-mode classifier |
| Run archive | S3-compatible bucket (optional) | keeps finished runs off the machine |

## 2. Evaluation instance

Run it next to production with its own port, config and trace directory:

```
cp deploy/pacds.yaml deploy/pacds-eval.yaml
cat >> deploy/pacds-eval.yaml <<'EOF'
trace:
  dir: "${PACDS_TRACE_DIR}"
  enabled_for: development
  replay_from: "${PACDS_REPLAY_DIR}"
EOF
sudo mkdir -p /srv/pacds-eval/traces && sudo chown 10001 /srv/pacds-eval/traces
# in deploy/.env: PACDS_EVAL_TRACE_DIR=/srv/pacds-eval/traces  PACDS_EVAL_PORT=8081
docker compose -p pacds-eval -f deploy/compose.yaml -f deploy/compose.eval.yaml --env-file deploy/.env up -d
```

Traces contain source code and log content: keep the directory, and every run directory, as private as the
repositories. `logs.allowed_hosts` (and `private_hosts`) in `pacds-eval.yaml` must list the log store's host, and
`clients` must let the harness's token subject ask about the cases' repositories.

## 3. Harness environment

```
uv sync                                   # on the harness machine
export PACDS_URL=http://pacds-vm:8081     # or pass --target
# Token: client credentials for a service client of the corporate issuer (aud must be pacds)
export PACDS_OIDC_TOKEN_URL=https://sso.corp.example/realms/it/protocol/openid-connect/token
export PACDS_OIDC_CLIENT_ID=pacds-eval PACDS_OIDC_CLIENT_SECRET=...     # optional PACDS_OIDC_SCOPE / _AUDIENCE
# Log store, as PACDS sees it; the upload endpoint only if this machine reaches it by another name
export PACDS_LOGS_S3_ENDPOINT=https://minio.corp.example PACDS_LOGS_S3_BUCKET=pacds-eval-logs
export PACDS_LOGS_S3_ACCESS_KEY=... PACDS_LOGS_S3_SECRET_KEY=...
# Client LLM (support agent, baseline, classifier)
export LLM_BASE_URL=http://vllm:8000/v1 LLM_MODEL=served-model-name LLM_API_KEY=... LLM_API=
export LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": true}}'   # optional, model-specific
# Where the evaluation PACDS writes traces, if this machine can read it (same VM or a mount)
export PACDS_TRACE_SOURCE_DIR=/srv/pacds-eval/traces
# Optional archive: PACDS_EVAL_ARCHIVE_S3_URI=s3://pacds-eval-runs/ PACDS_EVAL_ARCHIVE_ENDPOINT=https://minio.corp.example
#                   PACDS_EVAL_ARCHIVE_ACCESS_KEY_ID=... PACDS_EVAL_ARCHIVE_SECRET_ACCESS_KEY=... PACDS_EVAL_ARCHIVE_REGION=us-east-1
# Corporate CA for the harness's own connections
export SSL_CERT_FILE=/path/to/ca-bundle.pem AWS_CA_BUNDLE=/path/to/ca-bundle.pem
```

The log bucket must exist; `eval.sh` uploads the cases' logs to it (`--no-seed` skips that).

## 4. Runs

```
# PACDS alone on the fixed triage question, 3 repeats
./scripts/eval.sh --target $PACDS_URL --replay "--cases-dir /data/cases/crm --repeat 3"
# Support agent with and without PACDS, and the no-code baseline
./scripts/eval.sh --target $PACDS_URL --replay "--cases-dir /data/cases/crm --baseline" \
    --support "--cases-dir /data/cases/crm --variant full" --support "--cases-dir /data/cases/crm --variant no-pacds"
```

Each run gets `eval-runs/<UTC time>/` with results, client traces, the collected PACDS traces, `errors.json`,
`report/report.md`, and is archived when configured. A request that failed is found in the PACDS log by its
`request_id`. (Case sets outside the repository, `--cases-dir`, arrive with phase 3 of the corporate deployment
design; until then the harness reads `tests/replay/cases` and `--candidates`.)

## 5. After a run

```
python -m tests.analysis report RUN                       # already written by eval.sh
python -m tests.analysis classify RUN                     # failure modes of the misses (client LLM)
python -m tests.analysis compare RUN_A RUN_B              # two runs case by case, with McNemar
python -m tests.analysis select RUN --select misses       # the cases to look at or re-run
```

A milestone split across runs (usage windows, repeats) is `RUN1,RUN2,...` in `compare` and `select`, and
`report RUN1 RUN2 --out DIR`. A re-run of failed tickets (`--from-run RUN/support-1.json --select errors`) replaces
the failures when merged.

## 6. Cheaper iterations

- **Targeted runs**: `--from-run RUN --select misses|class=D|tier=certain|mode=wrong_questions`, plus the case set's
  regression sample (`regression-sample.json`, `python -m tests.analysis sample --write`).
- **Replay**: client calls replay with `--replay-from RUN`; PACDS calls replay only when the evaluation instance is
  started with the recordings (`PACDS_REPLAY_DIR` mounted to that run's `traces/pacds`), which the Compose dev stack
  does automatically.
