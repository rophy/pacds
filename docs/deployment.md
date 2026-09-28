# Deploying PACDS

PACDS runs as one container on a VM with Docker Compose, next to (not inside) a self-hosted LLM served by vLLM.
Everything site-specific is configuration: `deploy/pacds.yaml`, `deploy/.env` and `deploy/certs/`.

## 1. What PACDS needs

| Dependency | Requirement |
|---|---|
| LLM | An OpenAI-compatible `/v1/chat/completions` endpoint (vLLM). Context window of 128K is comfortable: the largest single request of an investigation measured 25K tokens median, 60K max. |
| vLLM flags | `--enable-auto-tool-choice --tool-call-parser <the model's parser>` (tool calling is required); `--enable-prefix-caching` (the investigation resends its growing transcript each turn); `--served-model-name` (goes in `PACDS_LLM_MODEL`). |
| Git | HTTPS access to the repositories clients ask about, with a read-only token per git host. |
| Log storage | S3-compatible storage (MinIO, Ceph, S3) serving presigned GET URLs that clients put in requests. |
| Identity | An OIDC issuer (Keycloak, Azure AD, ...) that issues access tokens with `aud` = `pacds` to the calling clients. |
| TLS | The corporate CA as a PEM file if internal hosts use it. |

## 2. Build the image

Build where PyPI and the base images are reachable (Python packages come from the URLs pinned in `uv.lock`), then
push to the internal registry:

```
docker build \
  --build-arg PYTHON_IMAGE=registry.corp.example/python:3.12-slim \
  --build-arg UV_IMAGE=registry.corp.example/astral-sh/uv:0.11.6 \
  -t registry.corp.example/pacds:2.0.0 .
docker push registry.corp.example/pacds:2.0.0
```

Behind a proxy, add `--build-arg HTTPS_PROXY=http://proxy.corp.example:3128`.

## 3. Configure

```
cp deploy/pacds.example.yaml deploy/pacds.yaml
cp deploy/.env.example deploy/.env
cp /path/to/corporate-ca.pem deploy/certs/corp-ca.pem     # only if needed
```

- **`deploy/.env`**: image, port, LLM URL/model/key, OIDC issuer, git token, `PACDS_CA_FILE=/etc/pacds/certs/corp-ca.pem`,
  proxy. It holds secrets: keep it out of version control (it is git-ignored) and readable only by the operator.
- **`deploy/pacds.yaml`**: which clients may ask about which repositories (`clients`), git hosts and their token
  variable (`git.credentials`), log storage hosts (`logs.allowed_hosts`, plus `logs.private_hosts` when the
  storage resolves to a private address), concurrency. Every option is commented in the sample.
- **Proxy**: the LLM client and git use `HTTPS_PROXY`/`NO_PROXY`; log storage and the OIDC issuer are always
  reached directly. Put internal hosts (vLLM, git) in `NO_PROXY` if they must not go through the proxy.

## 4. Run and check

```
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d
curl -s http://localhost:8080/healthz          # {"status":"ok"}
docker compose -f deploy/compose.yaml logs -f pacds
```

A first real request needs a token from the issuer and a repository the client may use; the evaluation harness
sends them (evaluation runbook: phase 4 of docs/superpowers/specs/2026-09-29-corporate-deployment-design.md). Every request writes one `pacds.audit` JSON line to the container log:
subject, repository, commit, questions, answers, usage, status and error code, keyed by `request_id` (also the
`x-typesafe-request-id` response header), which the other log lines of that request carry too.

## 5. Operate

- **Upgrade**: push the new image, change `PACDS_IMAGE`, `docker compose ... up -d`.
- **Git cache**: the `git-cache` volume keeps fetched repositories between requests; delete the volume to reclaim
  space (`docker compose ... down -v`), it refills on demand.
- **Capacity**: `limits.max_concurrent_evaluations` caps parallel investigations (others get 429); size it to the
  vLLM server. `llm.time_budget_seconds` bounds one investigation; `llm.timeout_seconds` one model call.
- **Model quirks**: if the model's tool parser rejects JSON-schema answers, set `llm.structured_outputs: false`;
  model-specific request fields (for example enabling thinking) go in `llm.extra_body`.

## 6. Security

- The container runs read-only, as a non-root user, with all capabilities dropped.
- Log URLs must be HTTPS on an allowed host; hosts may resolve to private addresses only when listed in
  `logs.private_hosts`. Redirects are never followed.
- Traces and replay (`trace:`) are development-only; the production config refuses them.
- Questions, documents, code and logs are treated as untrusted input by the investigation; answers are typed
  values only, never free text.

## 7. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Container unhealthy, log says `config references unset environment variable` | a `${...}` in `pacds.yaml` has no value in `.env` |
| 401 `unauthorized` | token issuer or `aud` does not match `auth.issuers`; issuer unreachable for JWKS |
| 403 `forbidden` | the token's subject has no `clients` entry covering the repository |
| 502 `git_fetch_failed` | token, proxy, or CA for the git host (`CERTIFICATE`/`server verification failed` in the log) |
| 422 `log_host_not_allowed` | log host not in `allowed_hosts`, or resolves to a private address without `private_hosts` |
| 504 `agent_budget_exceeded` | investigation out of turns or time; raise `time_budget_seconds` for slow GPUs |
| 500 `malformed_answer` | the model's final JSON does not match the questions; try `structured_outputs: false` |
