# Evaluation runbook

How to measure PACDS in a corporate environment: an evaluation instance of PACDS next to production, case sets of
real tickets, and the harness in this repository driving them. Design: docs/superpowers/specs/2026-09-28-eval-analysis-framework-design.md.
Deploying PACDS itself: docs/deployment.md.

## 1. Pieces and prerequisites

| Piece | Where | Role |
|---|---|---|
| Evaluation PACDS | `deploy/compose.yaml` + `deploy/compose.eval.yaml` on the VM | the service under test, writing a trace per request |
| Log store | S3-compatible (MinIO/Ceph) | holds the cases' log files; the harness hands PACDS presigned URLs |
| Harness | this repository, checked out on a machine that reaches PACDS, the store and the LLM | sends requests, runs the support agent and baseline, analyses |
| Client LLM | vLLM (`LLM_*`) | the support agent, the no-code baseline, LLM case reviews and the failure-mode classifier |
| Run archive | S3-compatible bucket (optional) | keeps finished runs off the machine |

Before starting, have:

- a production-style PACDS deployment that works (docs/deployment.md, including the `check_llm` preflight);
- an OIDC client for the harness (client credentials) whose tokens carry `aud` = `pacds`, and its `sub`
  (docs/deployment.md, section 4) for the evaluation config's `clients`;
- a log bucket that exists, with credentials that can write to it, reachable from the harness (upload) and from
  PACDS (presigned GET);
- on the harness machine: git, curl, Python 3.12 and [uv](https://docs.astral.sh/uv/) (or their internal mirrors),
  and this repository checked out. Every `python -m ...` command below runs from the repository root through
  `uv run` (or inside the environment `uv sync` created);
- a client LLM that supports tool calling (support agent) and `response_format: json_schema` (baseline, reviews,
  classifier), or set `LLM_STRUCTURED_OUTPUTS=false` to put the schemas in the prompt instead.

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
docker compose -p pacds-eval -f deploy/compose.yaml -f deploy/compose.eval.yaml --env-file deploy/.env up -d --wait
```

In `pacds-eval.yaml`: `logs.allowed_hosts` (and `private_hosts`) must list the log store's host as the harness's
`PACDS_LOGS_S3_ENDPOINT` names it, and `clients` must give the harness's token `sub` the cases' repositories
(`git.corp.example/**` for a whole host).

Traces contain source code and log content: keep the directory, and every run directory, as private as the
repositories. The instance writes one trace per request and never deletes them; once a run has collected its traces
(section 5), delete old ones, e.g. `find /srv/pacds-eval/traces -name '*.json' -mtime +30 -delete`.

## 3. Harness environment

```
uv sync
export PACDS_URL=http://pacds-vm:8081          # the evaluation instance; eval.sh needs --target $PACDS_URL too
export PACDS_CASES_DIR=/data/cases/crm         # the case set (section 4): seeding and every runner use it
# Token: client credentials for the harness's OIDC client (aud must be pacds)
export PACDS_OIDC_TOKEN_URL=https://sso.corp.example/realms/it/protocol/openid-connect/token
export PACDS_OIDC_CLIENT_ID=pacds-eval PACDS_OIDC_CLIENT_SECRET=...     # optional PACDS_OIDC_SCOPE / _AUDIENCE
# Log store, as PACDS sees it; the upload endpoint only if this machine reaches it by another name
export PACDS_LOGS_S3_ENDPOINT=https://minio.corp.example PACDS_LOGS_S3_BUCKET=pacds-eval-logs
export PACDS_LOGS_S3_ACCESS_KEY=... PACDS_LOGS_S3_SECRET_KEY=...
# Client LLM (support agent, baseline, reviews, classifier)
export LLM_BASE_URL=http://vllm:8000/v1 LLM_MODEL=served-model-name LLM_API_KEY=... LLM_API=
export LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": true}}'   # optional, model-specific
export LLM_TIMEOUT_SECONDS=300                 # optional: per model call, for a slow server (default 120-300 by tool)
export LLM_STRUCTURED_OUTPUTS=false            # only if the client model rejects json_schema
# Where the evaluation PACDS writes traces, if this machine can read it (same VM or a mount; section 5 otherwise)
export PACDS_TRACE_SOURCE_DIR=/srv/pacds-eval/traces
# Optional archive: PACDS_EVAL_ARCHIVE_S3_URI=s3://pacds-eval-runs/ PACDS_EVAL_ARCHIVE_ENDPOINT=https://minio.corp.example
#                   PACDS_EVAL_ARCHIVE_ACCESS_KEY_ID=... PACDS_EVAL_ARCHIVE_SECRET_ACCESS_KEY=... PACDS_EVAL_ARCHIVE_REGION=us-east-1
# Corporate CA for the harness's own connections: a full bundle (system CAs plus the corporate CA), not the corporate
# CA alone, or public hosts stop verifying
export SSL_CERT_FILE=/path/to/ca-bundle.pem AWS_CA_BUNDLE=/path/to/ca-bundle.pem
```

A static token works too: `PACDS_TOKEN=...` instead of the `PACDS_OIDC_*` variables. `eval.sh` uploads the case
set's logs to the bucket before each run (`--no-seed` skips that).

To record the evaluation instance's configuration (model, limits) in each run, write it once on the VM and copy it
to the harness machine:

```
docker compose -p pacds-eval -f deploy/compose.yaml -f deploy/compose.eval.yaml --env-file deploy/.env \
  exec -T pacds python -m pacds.devtools.show_config > pacds-eval-config.json      # API key redacted
```

## 4. Case sets

A case set is a directory kept with the other private evaluation data, never in this repository:
`<set>/<case id>/case.json` plus the case's log files, `CATALOG.md` and `regression-sample.json`. Export tickets from
the ticket system into JSON Lines, a JSON array or CSV with only these fields (any other column is refused):

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | letters, digits, `.`, `_`, `-` (e.g. the ticket key) |
| `repo` | yes | HTTPS git URL of the application the ticket is about |
| `ref` | yes | the deployed version when the ticket was reported: a tag or a full 40-character commit id. Not a branch (its head today may contain the fix) and not a short commit id (not resolved) |
| `report` | yes | the user's report as first written, before any diagnosis |
| `logs` | no | log files attached to the report, any file names, paths relative to the export (CSV: separated by `;`) |
| `resolution` | no, but needed to label | how the ticket was resolved: the conclusion and the change that fixed it. Shown to reviewers only, never to PACDS or the agent |
| `created_at` | no | report date as `YYYY-MM-DD` (compared with model training cutoffs in the catalog) |
| `source`, `url` | no | origin and link |

Then, with `PACDS_CASES_DIR` exported (section 3):

```
uv run python -m pacds_eval.casebook import /data/exports/crm.jsonl       # draft cases; logs copied in
uv run python -m pacds_eval.casebook review-llm                           # two blind LLM reviews per case
uv run python -m pacds_eval.casebook packet CRM-101 > packet.md           # or: a person reviews from the packet...
uv run python -m pacds_eval.casebook review CRM-101 --by alice --class D --confidence certain --fix "..." --evidence "..."
uv run python -m pacds_eval.casebook label                                # agreement labels; disagreement is disputed
uv run python -m pacds_eval.casebook adjudicate CRM-101 --class B --by carol --note "..."   # a person decides disputes
./scripts/eval.sh --target $PACDS_URL --replay "--baseline --repeat 3"      # the no-code baseline (client LLM only)
uv run python -m pacds_eval.casebook screen --from-run eval-runs/<that run>   # hard / clear
uv run python -m pacds_eval.analysis sample --write                              # regression sample
uv run python -m pacds_eval.catalog --cases-dir $PACDS_CASES_DIR --cutoff served-model=2026-01-31
uv run python -m pacds_eval.casebook status
```

Reviews label a case by where the fix was made, with the rules of the support playbook
(`pacds_eval/skills/tech-support/SKILL.md`); the reviewer sees the ticket, its logs and its resolution,
never other reviews. Two agreeing reviews label a case (`certain` when both are certain, else `probable`); both
saying `drop` reject it (no stated fix, several problems, not about the application). LLM reviews are fast and were
right on every Debezium ticket they labeled in a trial, but they are strict: a resolution that does not state the fix
gets dropped. Mixing one person and one LLM review per case is a good default for a new ticket source: people review
first, then `review-llm --reviewers 1` adds one LLM review to each case. Only labeled cases are evaluated.

## 5. Runs

Start with one case to check the whole path (token, repository, logs, model) before a milestone:

```
./scripts/eval.sh --target $PACDS_URL --pacds-config pacds-eval-config.json --replay "--case CRM-101"
```

A milestone, the same commands every time so runs stay comparable:

```
# PACDS alone on the fixed triage question, 3 repeats
./scripts/eval.sh --target $PACDS_URL --pacds-config pacds-eval-config.json --replay "--repeat 3"
# Support agent with and without PACDS, and the no-code baseline
./scripts/eval.sh --target $PACDS_URL --pacds-config pacds-eval-config.json --replay "--baseline" \
    --support "--variant full" --support "--variant no-pacds"
```

`--target` is what makes `eval.sh` use the deployed instance; without it, it starts the Compose dev stack.
`--cases-dir DIR` on `eval.sh` sets `PACDS_CASES_DIR` for one run. Each run gets `eval-runs/<UTC time>/` with
results (`replay-N.json`, `support-N.json`, numbered in the order of the options), client traces, the collected PACDS
traces, `errors.json`, `report/report.md`, and is archived when configured.

- **Failed requests**: `errors.json` lists them with their `request_id`; PACDS's log lines for one are found on the
  VM with
  `docker compose -p pacds-eval -f deploy/compose.yaml -f deploy/compose.eval.yaml --env-file deploy/.env logs pacds | grep request=<id>`
  (docs/deployment.md, section 7, lists the error codes).
- **PACDS traces when the harness is elsewhere**: copy the trace directory to the harness machine (or mount it), then
  `uv run python -m pacds_eval.runs collect-traces RUN /path/to/traces` and rerun `uv run python -m pacds_eval.analysis report RUN`.
- **Failure modes** (why a miss happened) need the support agent's `--variant full` run and its PACDS traces.

## 6. After a run

```
uv run python -m pacds_eval.analysis report RUN                       # already written by eval.sh
uv run python -m pacds_eval.analysis classify RUN                     # failure modes of the misses (client LLM)
uv run python -m pacds_eval.analysis compare RUN_A RUN_B              # two runs case by case, with McNemar
uv run python -m pacds_eval.analysis select RUN --select misses       # the cases to look at or re-run
uv run python -m pacds_eval.analysis context RUN --budget 32000       # tokens, cache and largest prompt under context policies
```

A milestone split across runs (usage windows, repeats) is `RUN1,RUN2,...` in `compare` and `select`, and
`report RUN1 RUN2 --out DIR`. A re-run of failed tickets (`--support "--from-run RUN/support-1.json --select errors"`)
replaces the failures when merged, as long as its results file has the same name as the original: results are named
by the position of the option (`support-1.json` for the first `--support`), so keep the same order of options.

## 7. Cheaper iterations

- **Targeted runs**: `--from-run RUN --select misses|class=D|tier=certain|mode=wrong_questions`, plus the case set's
  regression sample (`regression-sample.json`, `uv run python -m pacds_eval.analysis sample --write`).
- **Replay**: `eval.sh --replay-from RUN` answers every client model request identical to one recorded in RUN with
  the recorded response. For PACDS's own model calls, start the evaluation instance with that run's PACDS traces:

  ```
  mkdir -p /srv/pacds-eval/replay && cp -r RUN/traces/pacds /srv/pacds-eval/replay/<run>    # on the VM
  # in deploy/.env: PACDS_EVAL_REPLAY_DIR=/srv/pacds-eval/replay/<run>
  docker compose -p pacds-eval -f deploy/compose.yaml -f deploy/compose.eval.yaml -f deploy/compose.eval-replay.yaml \
    --env-file deploy/.env up -d --wait
  ```

  Only work whose request changed then calls the LLM. Start the instance without `compose.eval-replay.yaml` again for
  live runs.

## 8. Claude Code backend (subscription)

For development and evaluation, `LLM_API=claude_code` runs the model calls through the Claude Code CLI (`claude -p`) on
a Claude subscription instead of an API endpoint. PACDS, the support agent, the replay baseline, reviews and the
classifier all support it; `LLM_BASE_URL` and `LLM_API_KEY` are not used. Design:
docs/superpowers/specs/2026-09-30-claude-code-backend-design.md.

- **Token**: run `claude setup-token` for the long-lived token PACDS in the Compose stack uses. The clients (support
  agent, baseline, reviews, classifier) run the host's `claude`: with `CLAUDE_CODE_OAUTH_TOKEN` when it is set (`eval.sh`
  sources `.env`, so it is), else the host's own `claude` login.
- **`.env`** (or the environment):

  ```
  LLM_API=claude_code
  LLM_MODEL=haiku                 # or sonnet, opus, a full model id
  CLAUDE_CODE_OAUTH_TOKEN=...     # from claude setup-token
  ```

  `eval.sh` then builds the PACDS image with the CLI (`CLAUDE_CODE_VERSION` defaults to the host's version) and passes
  the token to the `pacds` service. A plain `docker compose up` does not set `CLAUDE_CODE_VERSION`: put it in `.env`
  (e.g. the output of `claude --version`) or the image has no CLI. The token is never written to `run.json` (unit-tested);
  PACDS's code only passes it through the environment to the CLI and never logs it.
- **Preflight**: `uv run python -m pacds.devtools.check_llm CONFIG` with a config file that sets `llm.api: claude_code` (and
  `CLAUDE_CODE_OAUTH_TOKEN` or the host login).
- **Replay is per session**: a recorded session is reused when its whole request is unchanged. Support-agent tickets
  with PACDS never replay.
- **Rate limits**: a subscription limits usage per window; lower `--concurrency` if runs hit 429s.
- **Not for shared deployments**: a subscription must not back a shared or production PACDS; use an API endpoint there.
- **`pacds_eval.analysis context`** does not apply to this backend.
