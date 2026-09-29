# Deploying PACDS

PACDS runs as one container on a VM with Docker Compose, next to (not inside) a self-hosted LLM served by vLLM.
Everything site-specific is configuration: `deploy/pacds.yaml`, `deploy/.env` and `deploy/certs/`.

## 1. What PACDS needs

| Dependency | Requirement |
|---|---|
| LLM | An OpenAI-compatible `/v1/chat/completions` endpoint (vLLM). Context window of 128K is comfortable: the largest single request of an investigation measured 25K tokens median, 60K max. |
| vLLM settings | See section 1.1. |
| Git | HTTPS access to the repositories clients ask about, with a read-only token per git host. |
| Log storage | S3-compatible storage (MinIO, Ceph, S3) serving presigned GET URLs that clients put in requests. |
| Identity | An OIDC issuer (Keycloak, Azure AD, ...) that issues access tokens with `aud` = `pacds` to the calling clients. |
| TLS | The corporate CA as a PEM file if internal hosts use it. |

### 1.1 The LLM server (vLLM)

PACDS drives an investigation as a tool-calling conversation (search code, read files, read git history, read logs),
then asks for the answers as JSON. The server must support:

| Capability | vLLM | If it is missing |
|---|---|---|
| Tool calling | `--enable-auto-tool-choice --tool-call-parser <parser for the model family>` (see vLLM's tool-calling docs for the parser names); a model trained for tool use | investigations cannot run |
| Context length | `--max-model-len` of at least 65536; 131072 is comfortable. The largest single request measured was 60K tokens (median 25K) | long investigations fail with a context-length error |
| JSON-schema answers | guided decoding for `response_format: json_schema` (on by default in vLLM) | set `llm.structured_outputs: false`: the schema goes in the prompt and PACDS validates and retries the answer |
| Output length | answers are short, but reasoning models think first: keep `llm.max_output_tokens` at 8192, or 16384+ for a reasoning model | "final answer did not complete: length" |
| Prefix caching | on by default in recent vLLM; keep it on (`--enable-prefix-caching` on older versions) | each turn re-reads the whole transcript: slower, same answers |
| Concurrency | `--max-num-seqs` at least `limits.max_concurrent_evaluations` (plus the evaluation's clients when they share the server) | requests queue; investigations may hit `time_budget_seconds` |

Model-specific request fields go in `llm.extra_body`, for example `{chat_template_kwargs: {enable_thinking: true}}`
for models whose chat template switches thinking on and off. Reasoning text that the server returns in a separate
field is ignored.

**Check the server before anything else.** With the container running:

```
docker compose -f deploy/compose.yaml --env-file deploy/.env exec pacds python -m pacds.devtools.check_llm
```

It uses PACDS's own configuration and checks a plain answer, a tool-call round trip, a JSON-schema answer and a
64K-token request, and says what to change for each failure. Example against a server that ignores JSON schemas:

```
  ok   chat         answered 'ready'
  ok   tools        called lookup({"key":"sky"}) and used the result
  FAIL json_schema  answer does not match the schema -> ... set llm.structured_outputs: false
  ok   context      63,902 tokens in 5.4s
```

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
docker compose -f deploy/compose.yaml --env-file deploy/.env exec pacds python -m pacds.devtools.check_llm
docker compose -f deploy/compose.yaml logs -f pacds
```

A first real request needs a token from the issuer and a repository the client may use; the evaluation harness
sends them (docs/evaluation-runbook.md). Every request writes one `pacds.audit` JSON line to the container log:
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
