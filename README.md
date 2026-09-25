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

## Development

```bash
uv sync
uv run pytest                              # unit + contract tests
./scripts/dev-setup.sh                     # Kind cluster "pacds" (real LLM from .env, else a fake one)
skaffold dev --kube-context kind-pacds     # PACDS on http://localhost:3002
uv run pytest -m e2e                       # smoke + exfiltration audit against the cluster
```

The LLM endpoint must be OpenAI-compatible and support tool calling and `response_format` JSON schema.
