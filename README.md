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

## Writing good questions

PACDS is a general service: it investigates the code and logs and returns typed answers, but the
meaning of each answer comes from the client's question. Answer quality depends on the question:

- Make choice options mutually exclusive, and separate them by a concrete test (e.g. "something the
  operator of this deployment runs or configures" vs "software the operator does not control").
- Say in each option what evidence supports it (e.g. "choose only when the faulty logic is
  identified; behavior the code produces deliberately is not a bug").
- Put your own categories and policy in the question; PACDS does not know them.

`tests/replay/harness.py` (`QUESTION`, `CRITERIA`) is a worked example for incident triage.

## Development

```bash
uv sync
uv run pytest                              # unit + contract tests

./scripts/e2e.sh                           # e2e: start the Compose stack, seed logs, run the tests, remove the stack
./scripts/eval.sh --audit                  # real-LLM evaluations (costs LLM usage; LLM_* from .env or the environment)
```

The dev stack (`compose.yaml`) runs PACDS with a mock OIDC provider that issues client tokens ([oidc-mock](https://github.com/rophy/oidc-mock)), a dev S3 (MinIO) for log URLs, and a fake LLM. PACDS uses the LLM from `.env` (see `.env.example`), or the fake LLM without it.

Two scripts start the stack, seed the logs, run from the host and remove the stack when everything passed. Both take `--reuse` (use a running stack, never remove it) and `--keep` (keep the stack after the run).

- `e2e.sh`: functional regression, pass/fail. Always uses the fake LLM, even when `.env` exists, so it costs nothing.
- `eval.sh`: real-LLM evaluations with the LLM from `LLM_*` (the environment, or `.env`). `--audit` runs the exfiltration audit (pass/fail); `--replay "<harness args>"` runs the replay harness and `--support "<runner args>"` the support agent (both repeatable; they report accuracy and never fail on it).

Dev loop:

```bash
docker compose up -d --build --wait        # PACDS on http://localhost:3002, oidc-mock on http://localhost:3003 (LLM from .env, else fake)
./scripts/seed-logs.sh                     # once per stack: e2e and replay log fixtures into the dev S3
uv run python -c "from tests.oidc import token; print(token())"   # a client token (sub "triage-agent")
./scripts/e2e.sh --reuse                   # run e2e against it (a fake-LLM stack), or ./scripts/eval.sh --reuse ... (a real-LLM one)
docker compose down -v                     # remove the stack
```

Replay evaluation (`tests/replay/`): real GitHub issues with known causes, in a `clear` and a `hard` set, scored by `python -m tests.replay.harness` (add `--baseline` to answer without the code, for comparison).

The LLM endpoint must be OpenAI-compatible (chat completions, or the Responses API with `LLM_API=responses`) and support tool calling and JSON-schema structured output.
