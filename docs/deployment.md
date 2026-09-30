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
| TLS | The corporate CA as a PEM file if internal hosts use it. PACDS itself serves plain HTTP: put it behind the corporate TLS reverse proxy or load balancer if clients must reach it over HTTPS. |
| VM | Docker Engine with Compose v2.24 or later (the evaluation overlay uses `!override`), a login to the internal registry (`docker login registry.corp.example`). |

Network paths to open:

| From | To | For |
|---|---|---|
| PACDS VM | vLLM, git host(s), log storage, OIDC issuer (its discovery document and JWKS) | every request |
| Clients (and the evaluation harness) | PACDS port, OIDC issuer token endpoint, log storage (to upload logs) | sending requests |
| Build machine | base images, PyPI and Debian package mirrors (or a proxy to them), internal registry | building the image |

### 1.1 The LLM server (vLLM)

PACDS drives an investigation as a tool-calling conversation (search code, read files, read git history, read logs),
then asks for the answers as JSON. The server must support:

| Capability | vLLM | If it is missing |
|---|---|---|
| Tool calling | `--enable-auto-tool-choice --tool-call-parser <parser for the model family>` (see vLLM's tool-calling docs for the parser names); a model trained for tool use | investigations cannot run |
| Context length | `--max-model-len` of at least 64K plus `llm.max_output_tokens` (the prompt and the output must fit together): 81920 with the sample's 8192 and 16384 for reasoning models; 131072 is comfortable. The largest single request measured was 60K tokens (median 25K) | long investigations fail (500 `engine_error`, "maximum context length" in the log) |
| JSON-schema answers | guided decoding for `response_format: json_schema` (on by default in vLLM) | set `llm.structured_outputs: false`: the schema goes in the prompt and PACDS validates and retries the answer |
| Output length | answers are short, but reasoning models think first: keep `llm.max_output_tokens` at 8192, or 16384+ for a reasoning model | "final answer did not complete: length" |
| Prefix caching | on by default in recent vLLM; keep it on (`--enable-prefix-caching` on older versions). Each turn resends the transcript (85% of an investigation's input); with the cache only the new part is computed, including for the final answer, which keeps the tools with `tool_choice: none` so its prompt starts the same (vLLM applies the JSON schema outside the prompt; a hosted API that renders the schema into the prompt misses the cache on the final request unless `structured_outputs: false`) | each turn re-reads the whole transcript: about twice the prefill work, slower, same answers |
| `tool_choice: none` | supported by vLLM; the tools stay in the prompt unless the server runs with `--exclude-tools-when-tool-choice-none` (leave that off) | set `llm.final_keeps_tools: false`: correct answers, but the final request is computed in full |
| Concurrency | `--max-num-seqs` at least `limits.max_concurrent_evaluations` (plus the evaluation's clients when they share the server) | requests queue; investigations may hit `time_budget_seconds` |

**Smaller context windows.** An investigation's largest prompt measured 25K tokens median, 38K p90 and 60K max. For
a server with less room than 64K plus `max_output_tokens`, set `llm.context_budget_tokens` (e.g. 32000): above it the
oldest large tool results are replaced by a short note that the model can act on by calling the tool again, keeping the
latest four. On the milestone's traces a 32K budget touched 51 of 227 investigations and capped the largest prompt at
32K; it does not save compute when prefix caching is on (every removal restarts the cache), so leave it unset when the
window is large enough. `python -m pacds_eval.analysis context RUN --budget N` shows what a budget would do to a run's
investigations (docs/evaluation-runbook.md).

Model-specific request fields go in `llm.extra_body`, for example `{chat_template_kwargs: {enable_thinking: true}}`
for models whose chat template switches thinking on and off. Reasoning text that the server returns in a separate
field is ignored.

**Check the server before starting PACDS** (once the image is pulled and the configuration written, sections 2
and 3), in a one-off container with PACDS's own configuration:

```
docker compose -f deploy/compose.yaml --env-file deploy/.env run --rm pacds check-llm
```

It checks a plain answer, a tool-call round trip, a JSON-schema answer with the tools present (as the final request
sends it) and a 64K-token request (`--context-tokens N`
to change it), and says what to change for each failure; the exit status is 1 when one fails. Example against a
server that ignores JSON schemas:

```
LLM served-model-name at http://vllm.corp.example:8000/v1 (chat_completions)
  ok   chat         answered 'ready' (served-model-name)
  ok   tools        called lookup({"key": "sky"}) and used the result
  FAIL json_schema  AssertionError: answer does not match the schema: {...} -> if the server or model rejects guided decoding, set llm.structured_outputs: false
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

The build also installs Debian packages (git) from the base image's mirrors. Behind a proxy, add
`--build-arg HTTP_PROXY=http://proxy.corp.example:3128 --build-arg HTTPS_PROXY=http://proxy.corp.example:3128`; with
an internal Debian mirror only, use a base image already pointed at it.

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
- **Repositories** (`clients[].repos`): patterns over host/path of the git URL without `.git`; `*` is one path
  segment and `**` any depth, so `git.corp.example/**` covers every repository on the host and
  `git.corp.example/crm/*` one group.
- **Proxy**: the LLM client and git use `HTTPS_PROXY`/`NO_PROXY`; log storage and the OIDC issuer are always
  reached directly. Put internal hosts (vLLM, git) in `NO_PROXY` if they must not go through the proxy.

## 4. Run and check

```
docker compose -f deploy/compose.yaml --env-file deploy/.env run --rm pacds check-llm
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --wait    # returns once /healthz answers
curl -s http://localhost:8080/healthz          # {"status":"ok"}
docker compose -f deploy/compose.yaml --env-file deploy/.env logs -f pacds
```

**Client identity.** Each calling client is an OIDC client of the corporate issuer (client credentials) whose access
tokens carry `aud` = `pacds` (Keycloak: an audience mapper on the client, or a client scope with one). PACDS matches
`clients[].subject` against the token's `sub` claim, which for Keycloak client credentials is the client's
service-account user id (a UUID), not the client id. Read it from a token:

```
TOKEN=$(curl -s -d grant_type=client_credentials -d client_id=pacds-eval -d client_secret=... \
  https://sso.corp.example/realms/it/protocol/openid-connect/token | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
python3 -c 'import base64,json,sys; p=sys.argv[1].split(".")[1]; c=json.loads(base64.urlsafe_b64decode(p+"="*(-len(p)%4))); print(c["sub"], c["aud"], c["iss"])' "$TOKEN"
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/v1/models    # smoke test: 200 with the engine name
```

`iss` must equal `auth.issuers[].issuer` exactly, `aud` must contain `pacds`, and `sub` goes in `clients[].subject`
(restart PACDS after editing `pacds.yaml`). A full request (repository checkout, logs, the model) is easiest from the
evaluation harness on one case (docs/evaluation-runbook.md, section 4).

**Which `ref` to send.** A tag or the full 40-character commit id of the deployed version. A branch name resolves to
the branch's current head, which may already contain the fix for the reported problem; a short commit id is not
resolved (422 `unknown_ref`). Every request writes one `pacds.audit` JSON line to the container log:
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
- Traces and replay (`trace:`) need `trace.enabled_for: development` next to the directory, so a production config
  cannot turn them on by one setting; only the evaluation instance's config sets both.
- Questions, documents, code and logs are treated as untrusted input by the investigation; answers are typed
  values only, never free text.

## 7. Troubleshooting

Every failed request has an error code in the response and in its `pacds.audit` line; the other log lines of the
request carry `request=<request_id>` and say more.

| Symptom | Likely cause |
|---|---|
| Container restarts repeatedly, log says `config references unset environment variable` | a `${...}` in `pacds.yaml` has no value in `.env` |
| 401 `unauthorized` | token expired, or its `iss`/`aud` does not match `auth.issuers` (section 4) |
| 502 `jwks_unavailable` | PACDS cannot fetch the issuer's discovery document or keys: network, proxy (the issuer is reached directly) or CA; `jwks_uri` skips discovery |
| 403 `forbidden` | no `clients` entry for the token's `sub` covers the repository (`*` vs `**`, section 3) |
| 500 `git_credentials_missing` | no `git.credentials` entry for the repository's host, or its `token_env` variable is empty |
| 502 `git_unavailable` | resolving the ref failed: token, proxy, or CA for the git host (`CERTIFICATE`/`server verification failed` in the log) |
| 502 `git_fetch_failed` | fetching the commit failed after the ref resolved: same causes, or a commit id that is not in the repository |
| 502 `git_timeout` | the fetch took longer than `git.timeout_seconds` (large repository, slow link) |
| 422 `unknown_ref` | the tag or branch does not exist, or a short commit id was sent (section 4) |
| 422 `repo_too_large` | the checkout exceeds `git.max_repo_size_mb` |
| 422 `log_host_not_allowed` | log host not in `allowed_hosts`, or resolves to a private address without `private_hosts` |
| 422 `log_fetch_rejected` | the storage answered the log URL with an error (expired presigned URL, wrong signature host) |
| 422 `log_too_large` / `log_redirect_refused` | a log above `logs.max_file_size_mb`; the storage redirected (never followed) |
| 502 `log_unreachable` | the log storage did not answer (network, CA, `fetch_timeout_seconds`) |
| 422 `invalid_request` | the request body is not a valid System One request (questions, `pacds.git`, logs) |
| 429 `rate_limited` | more than `limits.max_concurrent_evaluations` requests at once |
| 529 `overloaded` | the LLM server is unreachable or overloaded (wrong `base_url`, proxy, CA, server down); retry later |
| 500 `engine_error` | the LLM request failed otherwise, e.g. the context length exceeded (`--max-model-len`, section 1.1) |
| 504 `agent_budget_exceeded` | investigation out of turns or time; raise `time_budget_seconds` for slow GPUs |
| 500 `malformed_answer` / `invalid_answer` | the model's final JSON does not match the questions; try `structured_outputs: false` |
