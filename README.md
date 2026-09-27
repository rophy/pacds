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

./scripts/create-cluster.sh                # once: Kind cluster "kind-pacds" (Calico for NetworkPolicy)
./scripts/e2e.sh                           # e2e: create namespace "pacds", deploy, test in-cluster, delete namespace
```

`e2e.sh` runs the smoke tests and the exfiltration audit from an in-cluster test runner pod, so no port-forward is needed. It uses the LLM from `.env` (see `.env.example`), or an in-cluster fake LLM without it. Options: `--reuse` (test an existing deployment, never delete the namespace), `--keep` (keep the namespace after the run), `--replay "<harness args>"` (also run the replay harness).

Dev loop against the same cluster:

```bash
./scripts/dev-setup.sh                     # LLM config from .env
skaffold dev --kube-context kind-pacds     # PACDS on http://localhost:3002; ./scripts/seed-logs.sh once
./scripts/e2e.sh --reuse                   # run e2e against it
```

Replay evaluation (`tests/replay/`): real GitHub issues with known causes, in a `clear` and a `hard` set, scored by `python -m tests.replay.harness` (add `--baseline` to answer without the code, for comparison).

The LLM endpoint must be OpenAI-compatible (chat completions, or the Responses API with `LLM_API=responses`) and support tool calling and JSON-schema structured output.
