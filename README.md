# PACDS

PACDS answers one question for support teams: *is this production issue caused by code, by the user, or by the user's environment?*
It reads the application's source code and the incident's logs, but only ever returns **typed answers**. It implements
[TypeSafe's Jev System One API](https://docs.typesafe.ai/api.md), so no free text (and no code) can leave the service.

## API

`POST /v1/systemone` and `GET /v1/models`, exactly as TypeSafe documents them. Use the official SDK with `base_url` pointed at PACDS:

```python
from typesafe_sdk import Choice, TypeSafeClient

client = TypeSafeClient(api_key=service_account_token, base_url="https://pacds.example.internal", timeout=300)
response = client.system_one(
    state={
        "pacds": {
            "git": {"url": "https://git.example.com/shop/checkout-service.git", "ref": "v2.14.3"},
            "logs": [{"name": "server.log", "url": "<pre-signed HTTPS URL>"}],
        },
        "user_report": "Checkout fails with 'payment declined'",
    },
    questions={
        "cause": Choice(
            instructions="What caused this issue?",
            criteria={"code_defect": None, "user_action": None, "user_environment": None},
        )
    },
)
```

- **Auth:** `Authorization: Bearer <JWT>` from a configured OIDC issuer (Kubernetes projected ServiceAccount tokens with audience `pacds`). Re-read the token file before each request.
- **Repos:** each client subject is allowed a list of repo patterns; PACDS holds the git credentials. Pass the deployed tag or commit as `ref`.
- **Logs:** the client collects logs and passes pre-signed HTTPS URLs; hosts must be on the allow-list.
- **Timeouts:** investigations take tens of seconds to minutes; raise the SDK timeout (300 s recommended). Do not retry `504`.

Design: `docs/superpowers/specs/2026-09-25-pacds-jev-api-design.md`.

## What is published

Each release has one version (`version` in `pyproject.toml`) and three artifacts; nothing needs a checkout, Python or uv
on site.

| Artifact | Where | Content |
|---|---|---|
| Image | `ghcr.io/rophy/pacds:<version>` (also `<major>.<minor>` and `latest`; `linux/amd64`, `linux/arm64`) | `pacds serve`, `pacds check-llm`, the `pacds eval` toolkit (`run`, `audit`, `casebook`, `report`, ...), the support-agent sample at `/opt/pacds/samples/support-agent` |
| Deploy bundle | GitHub release `v<version>`, `pacds-deploy-<version>.tar.gz` | Compose files, `pacds.example.yaml`, `.env.example` pinned to the image, `certs/`, the deployment and evaluation docs |
| Samples bundle | same release, `pacds-samples-<version>.tar.gz` | the support-agent sample |

- **Deploy** on a VM with Docker Compose, a vLLM (or other OpenAI-compatible) server, a corporate OIDC issuer and CA:
  pull the image, unpack the deploy bundle, `docs/deployment.md` (preflight: `docker compose run --rm pacds check-llm`).
- **Evaluate** a deployed instance on your own tickets, and audit it for leaks, with the same image:
  `docs/evaluation-runbook.md` (case sets, runs, exfiltration audit, analysis).
- **Release**: raise `version` in `pyproject.toml` and merge to `master`. CI tests, builds the multi-arch image and the
  bundles, and pushes the image and creates tag `v<version>` and the release only when that version has no tag yet
  (otherwise it builds and skips publishing). To try it from a branch without publishing, run the `ci` workflow on the
  branch (Run workflow, `dry_run` on).

## Writing good questions

PACDS is a general service: it investigates the code and logs and returns typed answers, but the
meaning of each answer comes from the client's question. Answer quality depends on the question:

- Make choice options mutually exclusive, and separate them by a concrete test (e.g. "something the
  operator of this deployment runs or configures" vs "software the operator does not control").
- Say in each option what evidence supports it (e.g. "choose only when the faulty logic is
  identified; behavior the code produces deliberately is not a bug").
- Put your own categories and policy in the question; PACDS does not know them.

`src/pacds_eval/harness.py` (`QUESTION`, `CRITERIA`) is a worked example for incident triage.

## Development

```bash
uv sync
uv run pytest                              # unit + contract tests
uv run pacds --help                        # serve, check-llm, eval: the commands the image runs
uv run pacds eval --help

./scripts/e2e.sh                           # e2e: start the Compose stack, seed logs, run the tests, remove the stack
./scripts/eval.sh --audit                  # real-LLM evaluations (costs LLM usage; LLM_* from .env or the environment)
```

The dev stack (`compose.yaml`) runs PACDS with a mock OIDC provider that issues client tokens ([oidc-mock](https://github.com/rophy/oidc-mock)), a dev S3 (MinIO) for log URLs, and a fake LLM. PACDS uses the LLM from `.env` (see `.env.example`), or the fake LLM without it.

Two scripts start the stack, seed the logs, run from the host and remove the stack when everything passed. Both take `--reuse` (use a running stack, never remove it) and `--keep` (keep the stack after the run).

- `e2e.sh`: functional regression, pass/fail. Always uses the fake LLM, even when `.env` exists, so it costs nothing.
- `eval.sh`: real-LLM evaluations with the LLM from `LLM_*` (the environment, or `.env`). `--audit` runs the exfiltration audit (pass/fail); `--replay "<harness args>"` runs the replay harness and `--support "<runner args>"` the support agent (both repeatable; they report accuracy and never fail on it). Each run is recorded in `eval-runs/<UTC time>/`: `run.json` (commit, model, arguments), the results, `eval.log`, `compose.log` (the stack's logs, saved before teardown) and `errors.json` (every failed request with its request id; PACDS log lines carry `request=<id>`).

Dev loop:

```bash
docker compose up -d --build --wait        # PACDS on http://localhost:3002, oidc-mock on http://localhost:3003 (LLM from .env, else fake)
./scripts/seed-logs.sh                     # once per stack: e2e and replay log fixtures into the dev S3
uv run python -c "from pacds_eval.oidc import token; print(token())"   # a client token (sub "triage-agent")
./scripts/e2e.sh --reuse                   # run e2e against it (a fake-LLM stack), or ./scripts/eval.sh --reuse ... (a real-LLM one)
docker compose down -v                     # remove the stack
```

Replay evaluation (`src/pacds_eval/`): real support cases with known causes, in a `clear` and a `hard` set, scored by `uv run pacds eval replay` (add `--baseline` to answer without the code, for comparison). The case sets live in `cases/github/` (reviewed) and `cases/debezium/` (screened, unreviewed; run with `--cases-dir cases/debezium`); the package has no default set: pass `--cases-dir` or set `PACDS_CASES_DIR`. Each set's `CATALOG.md` lists every case with its source, creation date and each model's training cutoff; regenerate it with `uv run pacds eval catalog --cases-dir cases/<set>`.

The LLM endpoint must be OpenAI-compatible (chat completions, or the Responses API with `LLM_API=responses`) and support tool calling and JSON-schema structured output.

For development and evaluation, `LLM_API=claude_code` uses a Claude subscription through the Claude Code CLI instead: see `docs/evaluation-runbook.md`, section 9.
