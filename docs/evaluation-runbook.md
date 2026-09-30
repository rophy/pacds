# Evaluation runbook

How to measure PACDS in a corporate environment: an evaluation instance of PACDS next to production, case sets of
real tickets, and the harness driving them. The harness is the `pacds eval` toolkit in the PACDS image: no checkout,
Python or uv. Design: docs/superpowers/specs/2026-09-28-eval-analysis-framework-design.md.
Deploying PACDS itself: docs/deployment.md.

## 1. Pieces and prerequisites

| Piece | Where | Role |
|---|---|---|
| Evaluation PACDS | the deploy bundle's `compose.yaml` + `compose.eval.yaml` on the VM | the service under test, writing a trace per request |
| Log store | S3-compatible (MinIO/Ceph) | holds the cases' log files; the harness hands PACDS presigned URLs |
| Harness | `docker run ... ghcr.io/rophy/pacds:<version> eval ...` on a machine that reaches PACDS, the store and the LLM | sends requests, runs the support agent and baseline, audits, analyses |
| Client LLM | vLLM (`LLM_*`) | the support agent, the no-code baseline, LLM case reviews and the failure-mode classifier |
| Run archive | S3-compatible bucket (optional) | keeps finished runs off the machine |

Before starting, have:

- a production-style PACDS deployment that works (docs/deployment.md, including the `check-llm` preflight);
- an OIDC client for the harness (client credentials) whose tokens carry `aud` = `pacds`, and its `sub`
  (docs/deployment.md, section 4) for the evaluation config's `clients`;
- a log bucket that exists, with credentials that can write to it, reachable from the harness (upload) and from
  PACDS (presigned GET);
- on the harness machine: Docker and the PACDS image (`docker pull ghcr.io/rophy/pacds:2.1.0`, or from the internal
  registry; docs/deployment.md, section 2). Use the same version as the evaluation instance: a run warns when the
  toolkit's and the target's major versions differ;
- a client LLM that supports tool calling (support agent) and `response_format: json_schema` (baseline, reviews,
  classifier), or set `LLM_STRUCTURED_OUTPUTS=false` to put the schemas in the prompt instead.

## 2. Evaluation instance

Run it next to production with its own port, config and trace directory. In the unpacked deploy bundle
(docs/deployment.md, section 2):

```
cp pacds.yaml pacds-eval.yaml
cat >> pacds-eval.yaml <<'EOF'
trace:
  dir: "${PACDS_TRACE_DIR}"
  enabled_for: development
  replay_from: "${PACDS_REPLAY_DIR}"
EOF
sudo mkdir -p /srv/pacds-eval/traces && sudo chown 10001 /srv/pacds-eval/traces
# in .env: PACDS_EVAL_TRACE_DIR=/srv/pacds-eval/traces  PACDS_EVAL_PORT=8081
docker compose -p pacds-eval -f compose.yaml -f compose.eval.yaml --env-file .env up -d --wait
```

In `pacds-eval.yaml`: `logs.allowed_hosts` (and `private_hosts`) must list the log store's host as the harness's
`PACDS_LOGS_S3_ENDPOINT` names it, and `clients` must give the harness's token `sub` the cases' repositories
(`git.corp.example/**` for a whole host).

Traces contain source code and log content: keep the directory, and every run directory, as private as the
repositories. The instance writes one trace per request and never deletes them; once a run has collected its traces
(section 5), delete old ones, e.g. `find /srv/pacds-eval/traces -name '*.json' -mtime +30 -delete`.

## 3. Harness environment

Everything the toolkit reads from the environment goes in one file, `eval.env`, in Docker's `--env-file` format:
`NAME=value` lines, no `export`, no quotes (a value is taken literally, quotes included). It holds secrets: keep it
readable only by you (`chmod 600 eval.env`).

```
# The case set inside the container (section 4): seeding and every runner use it
PACDS_CASES_DIR=/cases
# Token: client credentials for the harness's OIDC client (aud must be pacds)
PACDS_OIDC_TOKEN_URL=https://sso.corp.example/realms/it/protocol/openid-connect/token
PACDS_OIDC_CLIENT_ID=pacds-eval
PACDS_OIDC_CLIENT_SECRET=...
# optional PACDS_OIDC_SCOPE / PACDS_OIDC_AUDIENCE; or a static token: PACDS_TOKEN=...
# Log store, as PACDS sees it (PACDS_LOGS_S3_UPLOAD_ENDPOINT only if this machine reaches it by another name)
PACDS_LOGS_S3_ENDPOINT=https://minio.corp.example
PACDS_LOGS_S3_BUCKET=pacds-eval-logs
PACDS_LOGS_S3_ACCESS_KEY=...
PACDS_LOGS_S3_SECRET_KEY=...
# Client LLM (support agent, baseline, reviews, classifier)
LLM_BASE_URL=http://vllm:8000/v1
LLM_MODEL=served-model-name
LLM_API_KEY=...
# LLM_API=                        (empty: chat completions; responses: the Responses API)
# LLM_EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": true}}   model-specific, optional
# LLM_TIMEOUT_SECONDS=300         per model call, for a slow server (default 120-300 by tool)
# LLM_STRUCTURED_OUTPUTS=false    only if the client model rejects json_schema
# Where the evaluation PACDS writes traces, as mounted below (section 5 otherwise)
PACDS_TRACE_SOURCE_DIR=/traces
# Optional archive of finished runs
# PACDS_EVAL_ARCHIVE_S3_URI=s3://pacds-eval-runs/
# PACDS_EVAL_ARCHIVE_ENDPOINT=https://minio.corp.example
# PACDS_EVAL_ARCHIVE_ACCESS_KEY_ID=...
# PACDS_EVAL_ARCHIVE_SECRET_ACCESS_KEY=...
# PACDS_EVAL_ARCHIVE_REGION=us-east-1
# Corporate CA for the harness's own connections: a full bundle (system CAs plus the corporate CA), not the corporate
# CA alone, or public hosts stop verifying; the file is mounted below
SSL_CERT_FILE=/etc/ssl/ca-bundle.pem
AWS_CA_BUNDLE=/etc/ssl/ca-bundle.pem
```

The container sees only what you mount. Define a shell alias once (add it to your shell profile) and use it for every
command below:

```
alias pacds-eval='docker run --rm --env-file eval.env -w /runs \
  -v /data/cases/crm:/cases -v /data/eval-runs:/runs -v /data/exports:/exports:ro \
  -v /srv/pacds-eval/traces:/traces:ro -v /etc/ssl/corp-ca-bundle.pem:/etc/ssl/ca-bundle.pem:ro \
  ghcr.io/rophy/pacds:2.1.0 eval'
export PACDS_URL=http://pacds-vm:8081          # the evaluation instance, for --target below
```

| Mount | Container path | Needs |
|---|---|---|
| the case set (section 4) | `/cases` | writable: `casebook`, `sample --write` and `catalog` write into it |
| run directories | `/runs`, the working directory: a run's default directory `eval-runs/<UTC time>` is `/runs/eval-runs/<UTC time>`, and the RUN arguments below are `eval-runs/<name>` | writable |
| ticket exports | `/exports` | read-only is enough; only for `casebook import` |
| the evaluation PACDS's traces | `/traces` | read-only; only when this machine can read them (same VM or a mount) |
| the CA bundle | the path in `SSL_CERT_FILE` | read-only; only with a corporate CA |

The container runs as user 10001, which must be able to write the writable mounts; what it writes is owned by that
user:

```
sudo mkdir -p /data/eval-runs && sudo chown 10001 /data/eval-runs /data/cases/crm
```

Use `sudo` to delete or edit what it wrote, and drop the mounts you do not need from the alias.
`pacds-eval run` seeds the case set's logs to the bucket before each run (`--no-seed` skips that).

To record the evaluation instance's configuration (model, limits) in each run, write it once on the VM and copy it to
`/data/eval-runs` on the harness machine (it is `/runs/pacds-eval-config.json` in the container):

```
docker compose -p pacds-eval -f compose.yaml -f compose.eval.yaml --env-file .env \
  exec -T pacds python -m pacds.devtools.show_config > pacds-eval-config.json      # API key redacted
```

## 4. Case sets

A case set is a directory kept with the other private evaluation data, never in the PACDS repository or image:
`<set>/<case id>/case.json` plus the case's log files, `CATALOG.md` and `regression-sample.json`. Mount it at `/cases`
(section 3). Export tickets from the ticket system into JSON Lines, a JSON array or CSV with only these fields (any
other column is refused), and put the export in the directory mounted at `/exports`:

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

Then, with `PACDS_CASES_DIR=/cases` in `eval.env` (section 3):

```
pacds-eval casebook import /exports/crm.jsonl                       # draft cases; logs copied in
pacds-eval casebook review-llm                                      # two blind LLM reviews per case
pacds-eval casebook packet CRM-101 > packet.md                      # or: a person reviews from the packet...
pacds-eval casebook review CRM-101 --by alice --class D --confidence certain --fix "..." --evidence "..."
pacds-eval casebook label                                           # agreement labels; disagreement is disputed
pacds-eval casebook adjudicate CRM-101 --class B --by carol --note "..."   # a person decides disputes
pacds-eval run --target $PACDS_URL --replay "--baseline --repeat 3"       # the no-code baseline (client LLM only)
pacds-eval casebook screen --from-run eval-runs/<that run>          # hard / clear
pacds-eval sample --write                                           # regression sample
pacds-eval catalog --cutoff served-model=2026-01-31                 # CATALOG.md
pacds-eval casebook status
```

Reviews label a case by where the fix was made, with the rules of the support playbook (the `tech-support` skill,
`/opt/pacds/samples/support-agent/skills/tech-support/SKILL.md` in the image); the reviewer sees the ticket, its logs and
its resolution, never other reviews. Two agreeing reviews label a case (`certain` when both are certain, else
`probable`); both saying `drop` reject it (no stated fix, several problems, not about the application). LLM reviews are
fast and were right on every Debezium ticket they labeled in a trial, but they are strict: a resolution that does not
state the fix gets dropped. Mixing one person and one LLM review per case is a good default for a new ticket source:
people review first, then `review-llm --reviewers 1` adds one LLM review to each case. Only labeled cases are evaluated.

## 5. Runs

Start with one case to check the whole path (token, repository, logs, model) before a milestone:

```
pacds-eval run --target $PACDS_URL --pacds-config /runs/pacds-eval-config.json --replay "--case CRM-101"
```

A milestone, the same commands every time so runs stay comparable:

```
# PACDS alone on the fixed triage question, 3 repeats
pacds-eval run --target $PACDS_URL --pacds-config /runs/pacds-eval-config.json --replay "--repeat 3"
# Support agent with and without PACDS, and the no-code baseline
pacds-eval run --target $PACDS_URL --pacds-config /runs/pacds-eval-config.json --replay "--baseline" \
    --support "--variant full" --support "--variant no-pacds"
```

`--cases-dir DIR` on `run` sets the case set for one run (a path inside the container). Each run gets
`eval-runs/<UTC time>/` (or the directory of `--run-dir`) with results (`replay-N.json`, `support-N.json`, numbered in
the order of the options), client traces, the collected PACDS traces, `errors.json`, `report/report.md`, and is archived
when configured. The exit status is the first failing step's; the run is finished (report, archive) either way.

- **Failed requests**: `errors.json` lists them with their `request_id`; PACDS's log lines for one are found on the
  VM, in the bundle directory, with
  `docker compose -p pacds-eval -f compose.yaml -f compose.eval.yaml --env-file .env logs pacds | grep request=<id>`
  (docs/deployment.md, section 7, lists the error codes).
- **PACDS traces when the harness is elsewhere**: copy the trace directory to the harness machine (or mount it at
  `/traces`), then `pacds-eval runs collect-traces eval-runs/<run> /traces` and rerun `pacds-eval report eval-runs/<run>`.
- **Failure modes** (why a miss happened) need the support agent's `--variant full` run and its PACDS traces.

## 6. Exfiltration audit

PACDS must return typed answers and nothing else, whatever a client asks. The audit sends every red-team prompt
(packaged with the toolkit) to a deployed PACDS with its real LLM, and fails when a response is not exactly the typed
shape or contains other text. Run it against the evaluation instance, and again after a change to the model or the
prompts:

```
pacds-eval audit --target $PACDS_URL --repo https://git.corp.example/crm/app.git --ref v3.2.1
```

`--repo` and `--ref` name a repository of your own and a tag or full commit id of it: the default is a public GitHub
repository, which the harness's client (`clients[].repos` in `pacds-eval.yaml`) and the instance's git credentials
probably do not cover. The audit uses the harness's token (`PACDS_OIDC_*` or `PACDS_TOKEN`) and needs no log storage.
`--vectors FILE` adds your own vectors (repeatable; same JSON schema as the packaged ones, checked before anything is
sent). It prints one line per vector (`ok`, `LEAK` or `FAIL`) and `n/total vectors passed`, writes `audit.json` (with
`complete` and `passed`) to the run directory (`--run-dir`, default `eval-runs/<UTC time>`), and exits 1 when any
vector leaks or fails, so it can gate a rollout.

## 7. After a run

```
pacds-eval report eval-runs/RUN                       # already written by run
pacds-eval classify eval-runs/RUN                     # failure modes of the misses (client LLM)
pacds-eval compare eval-runs/RUN_A eval-runs/RUN_B    # two runs case by case, with McNemar
pacds-eval select eval-runs/RUN --select misses       # the cases to look at or re-run
pacds-eval context eval-runs/RUN --budget 32000       # tokens, cache and largest prompt under context policies
pacds-eval runs list                                  # archived runs (needs the archive variables)
pacds-eval runs fetch RUN                             # download one into eval-runs/RUN
```

A milestone split across runs (usage windows, repeats) is `RUN1,RUN2,...` in `compare` and `select`, and
`report RUN1 RUN2 --out DIR`. A re-run of failed tickets (`--support "--from-run RUN/support-1.json --select errors"`)
replaces the failures when merged, as long as its results file has the same name as the original: results are named
by the position of the option (`support-1.json` for the first `--support`), so keep the same order of options.

## 8. Cheaper iterations

- **Targeted runs**: `--from-run RUN --select misses|class=D|tier=certain|mode=wrong_questions`, plus the case set's
  regression sample (`regression-sample.json`, `pacds-eval sample --write`).
- **Replay**: `pacds-eval run --replay-from RUN ...` answers every client model request identical to one recorded in
  RUN with the recorded response. For PACDS's own model calls, start the evaluation instance with that run's PACDS
  traces:

  ```
  mkdir -p /srv/pacds-eval/replay && cp -r RUN/traces/pacds /srv/pacds-eval/replay/<run>    # on the VM
  # in .env: PACDS_EVAL_REPLAY_DIR=/srv/pacds-eval/replay/<run>
  docker compose -p pacds-eval -f compose.yaml -f compose.eval.yaml -f compose.eval-replay.yaml \
    --env-file .env up -d --wait
  ```

  Only work whose request changed then calls the LLM. Start the instance without `compose.eval-replay.yaml` again for
  live runs.

## 9. Claude Code backend (subscription)

This section is for development machines with a repository checkout (`scripts/eval.sh` and the Compose dev stack); it
is not part of a deployment from the published image. `LLM_API=claude_code` runs the model calls through the Claude Code CLI (`claude -p`) on
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
- **Preflight**: `uv run pacds check-llm CONFIG` with a config file that sets `llm.api: claude_code` (and
  `CLAUDE_CODE_OAUTH_TOKEN` or the host login).
- **Replay is per session**: a recorded session is reused when its whole request is unchanged. Support-agent tickets
  with PACDS never replay.
- **Rate limits**: a subscription limits usage per window; lower `--concurrency` if runs hit 429s.
- **Not for shared deployments**: a subscription must not back a shared or production PACDS; use an API endpoint there.
- **`pacds eval context`** does not apply to this backend.
