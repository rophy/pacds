# PACDS v2: Jev-Compatible Diagnostic API — Design

**Date:** 2026-09-25
**Status:** Draft, pending review
**Supersedes:** `2026-09-16-pacds-design.md` (free-text diagnostic API)

## 1. Problem

Support staff need to answer a recurring question for end users: *is this production issue caused by code, by the user, or by the user's environment?* They want to give users an AI agent that can answer that on its own.

The answer depends on proprietary source code. The v1 design returned free-text findings, guarded by system-prompt rules, length limits and a code-likeness filter. Red-teaming found three bypasses ("correct my code", "sequence diagram", "compare colleague's code") that leak implementation details by *describing* the code rather than quoting it. A deny-list filter on free text cannot close that class of attack.

## 2. Roles

| Role | Description | Relation to PACDS |
|---|---|---|
| A. Developers | Know the code | Not direct users |
| B. Support team | Can read the code but don't understand it | Operate PACDS; build the client agent |
| C. End users | Use the deployed application | Interact with B's agent; never call PACDS directly |

## 3. Core Decision

PACDS exposes **exactly the TypeSafe Jev HTTP API** ([API reference](https://docs.typesafe.ai/api.md)): `POST /v1/systemone` and `GET /v1/models`. It never returns generated text; every answer is a typed value (Choice / Score / Noul) with probabilities.

- **Leak protection comes from the output contract.** Free text never leaves PACDS, so the description-based bypasses have no channel.
- **Future-proofing.** Clients use the official `typesafe-sdk` with `base_url` pointed at PACDS. The internal engine is swappable (LLM agent today; real Jev or an open-source System One engine later).
- **Engine.** TypeSafe's official MIT [`system-one-adapter-python`](https://github.com/typesafe-ai/system-one-adapter-python) provides the Jev semantics (schema building, output validation, corrective retries, probability normalization, confidence). PACDS supplies a custom provider that runs a multi-turn code/log search agent.

The existing TypeScript implementation (`packages/`) is replaced by a new Python project. Git history stays as reference.

## 4. Decisions Log

| # | Decision | Choice |
|---|---|---|
| 1 | Wire protocol | Exact Jev spec |
| 2 | Who writes questions | Caller, open questions (no catalog). Yes/no probing is an accepted risk, mitigated by auditing every request |
| 3 | Internal engine | LLM agent (multi-turn code search) behind the Jev spec, via the TypeSafe adapter |
| 4 | Logs | Client collects logs and attaches pre-signed HTTPS URLs. PACDS does not query log backends |
| 5 | Repo selection | Client passes git URL + ref in `state` |
| 6 | Git access | PACDS holds git credentials; authorizes each client per repo |
| 7 | Client identity | OIDC JWT only (Kubernetes projected ServiceAccount tokens first) |
| 8 | Private free-text note for support staff | Future work |

## 5. API

### 5.1 `POST /v1/systemone`

Request follows the Jev spec. PACDS-specific inputs live under the reserved key `state.pacds`:

```json
{
  "model": "pacds-1",
  "state": {
    "pacds": {
      "git":  { "url": "https://git.example.com/shop/checkout-service.git", "ref": "v2.14.3" },
      "logs": [ { "name": "server.log", "url": "https://bucket.s3.amazonaws.com/server.log?X-Amz-Signature=..." } ]
    },
    "user_report": "Checkout fails with 'payment declined' but my card works elsewhere",
    "client_info": { "browser": "Firefox 131" }
  },
  "questions": {
    "cause": {
      "type": "choice",
      "instructions": "What caused this issue?",
      "criteria": {
        "code_defect": "A bug in the application code",
        "user_action": "Invalid input or wrong sequence of steps",
        "user_environment": "User's browser, device, network or local settings",
        "service_environment": "Infrastructure, deployment config or external dependency"
      }
    }
  }
}
```

- `state` must be a JSON object containing `pacds`.
- `state.pacds.git.url` (https) and `state.pacds.git.ref` (branch, tag or commit) are required. Callers should pass the deployed tag or commit.
- `state.pacds.logs` is optional (max `logs.max_files` entries).
- Everything in `state` except `pacds` is passed to the agent as context.
- Question validation follows the Jev spec: types `noul` / `choice` / `score`; Choice 2–255 options; Score 2–10 levels.

Response follows the Jev spec: `{ "model", "answers", "usage" }`. `usage` sums tokens across all LLM calls of the agent run.

### 5.2 `GET /v1/models`

Returns the PACDS engine version(s) as Jev model cards (`name`, `description`, `release_date`).

### 5.3 Authentication

`Authorization: Bearer <JWT>`. The JWT is verified offline against configured OIDC issuers (signature via JWKS, `iss`, `aud`, `exp`). Client identity is the `sub` claim, e.g. `system:serviceaccount:support:triage-agent`.

Kubernetes clients mount a projected ServiceAccount token with `audience: pacds`. Tokens rotate (default 1h); clients must re-read the token file before each request.

### 5.4 Errors

| Status | When |
|---|---|
| 401 | Missing or invalid JWT |
| 403 | Subject not authorized for the requested git URL |
| 422 | Malformed request; missing `state.pacds.git`; unknown ref; log URL host not allowed; log too large; storage returned 4xx |
| 429 | Concurrent evaluation limit reached |
| 500 | Adapter exhausted corrective retries |
| 502 | Git server or log storage unreachable |
| 504 | Agent exceeded turn or time budget without answering |
| 529 | Upstream LLM overloaded or rate-limited |

Error bodies are JSON describing the offending field. They never include credentials, code or log content.

### 5.5 Client guidance

- Set SDK timeout well above the default 10 s (recommended: 300 s).
- SDK auto-retry of 429/529 is safe. Do not retry 504.
- Re-read the projected token per request.

## 6. Architecture

```
Client (TypeSafe SDK, base_url → PACDS)
   │ POST /v1/systemone  Authorization: Bearer <SA JWT>
   ▼
┌──────────────────────── PACDS (Python, FastAPI) ────────────────────────┐
│ api        Jev wire contract, request/response validation               │
│ auth       OIDC JWT verification → subject                              │
│ authz      subject × git URL → allow / 403                              │
│ workspace  git: shallow fetch url@ref with PACDS creds, cache by SHA    │
│            logs: https, host allow-list, size cap, timeout, no redirect │
│ engine     system-one-adapter  +  AgentProvider (ours)                  │
│              tools: search_code, read_file, list_files,                 │
│                     search_logs, read_log                               │
│              LLM: any OpenAI-compatible endpoint                        │
│ audit      one structured record per request                            │
└──────────────────────────────────────────────────────────────────────────┘
```

### 6.1 Components

- **api**: FastAPI app. Parses and validates the Jev request, orchestrates the flow, re-validates the response (exact question ids; choice ∈ defined options; numbers in range; no extra fields).
- **auth**: OIDC discovery + JWKS cache per issuer. Rejects `alg: none` and algorithm confusion. Behind an interface so other issuers need only config.
- **authz**: Matches the normalized git URL (host + path, no userinfo, no `..`, no trailing `.git` ambiguity) against the client's repo glob patterns.
- **workspace/git**: Resolves `ref` to a commit SHA with `git ls-remote`, shallow-fetches into `cache_dir/<host>/<path>/<sha>` if absent. Credentials are injected through a credential helper, never in URLs, argv, errors or logs. Enforces `max_repo_size_mb`.
- **workspace/logs**: Downloads each URL into a per-request temp dir. https only; host must match `allowed_hosts`; redirects are not followed; size capped while streaming; timeout per file; resolved IPs in private, loopback, link-local or metadata ranges are rejected.
- **engine**: `AsyncSystemOneAdapterClient` from `system-one-adapter`. A new `AgentProvider` instance is created per request, bound to that request's workspace. It implements the adapter's `AsyncProvider` protocol: `request(messages, schema, structured) -> ProviderResult`. Inside, it runs a tool-calling loop against the configured LLM, then produces the final JSON matching `schema`. Tools are read-only and confined to the workspace (path traversal and symlinks out of the workspace are rejected). The loop is bounded by `max_turns` and `time_budget_seconds`.
- **audit**: One JSON record per request to stdout: timestamp, subject, git URL, resolved SHA, log URL hosts (not signatures), questions, answers, usage, latency, status. Never includes code, log content, or JWTs.

### 6.2 Request flow

1. Parse and validate request → 422 on failure.
2. Verify JWT → 401.
3. Authorize subject for git URL → 403.
4. Prepare workspace (git and logs in parallel) → 422 / 502.
5. Remove `state.pacds`; call the adapter with an `AgentProvider` bound to the workspace → 500 / 504 / 529.
6. Re-validate answers; respond; clean up temp files; write audit record (for every outcome, including failures).

## 7. Configuration

YAML file mounted from a ConfigMap; secrets via environment variables.

```yaml
llm:
  base_url: ${PACDS_LLM_BASE_URL}
  model: ${PACDS_LLM_MODEL}
  api_key: ${PACDS_LLM_API_KEY}
  max_turns: 30
  time_budget_seconds: 180

auth:
  issuers:
    - issuer: https://kubernetes.default.svc.cluster.local
      audience: pacds

clients:
  - subject: system:serviceaccount:support:triage-agent
    repos: ["git.example.com/shop/*"]

git:
  credentials:
    - host: git.example.com
      token_env: GIT_TOKEN_EXAMPLE
  cache_dir: /var/cache/pacds/git
  max_repo_size_mb: 500

logs:
  allowed_hosts: ["*.s3.amazonaws.com", "storage.googleapis.com"]
  max_file_size_mb: 50
  max_files: 10
  fetch_timeout_seconds: 30

limits:
  max_concurrent_evaluations: 4
```

## 8. Deployment

- Single stateless `Deployment`; git cache on `emptyDir`.
- PACDS ServiceAccount bound to `system:service-account-issuer-discovery` to read the cluster JWKS.
- Container: Python 3.12, `uv`, non-root, read-only root filesystem, with `git` and `ripgrep`.
- Local dev: Kind (`kind-pacds`) + Skaffold, adapted to the Python image. aimock stays as the fake LLM. A sample `support/triage-agent` ServiceAccount; test tokens via `kubectl --context kind-pacds create token triage-agent -n support --audience pacds`.

## 9. Testing

### 9.1 Unit (pytest)
- **auth:** valid, expired, wrong audience, wrong issuer, bad signature, `alg: none`.
- **authz:** pattern matching and escapes (`shop/*` vs `shop-evil/…`, `shop/../x`, userinfo in URL).
- **git:** local bare-repo fixture; ref→SHA; cache reuse; unknown ref; credentials absent from errors and logs.
- **logs:** host allow-list, streaming size cap, timeout, https only, redirects not followed, private-IP rejection.
- **AgentProvider:** scripted fake LLM; turn limit; time budget; workspace confinement of tools.
- **Output validation:** extra keys, unknown options, out-of-range numbers, text in typed fields.

### 9.2 Contract
- Official `typesafe-sdk` pointed at PACDS via `base_url`: Noul, Choice, Score round-trips; error classes map correctly.
- [jevcompat](https://github.com/mandu5/jevcompat) conformance suite, adopted only if it runs cleanly (new, unproven project).

### 9.3 End-to-end (Kind)
- aimock LLM, real projected SA token, MinIO pre-signed URLs, private git repo served in-cluster.

### 9.4 Security audit (replaces `tests/security/` text-pattern audit)
- Existing attack prompts re-expressed as Jev requests (attacks in `state`, questions, and log files). Pass condition is structural: response contains only schema-valid typed values.
- New vectors: SSRF via log URLs (internal IPs, metadata endpoint, redirect chains), oversized logs, unauthorized git URLs, path traversal via tool calls.
- Yes/no probing documented as accepted risk; test asserts the audit record captures enough to detect it.

## 10. Accepted Risks

- **Probing with open questions.** A caller can ask targeted yes/no questions about the code (e.g. "does the code store card numbers unencrypted?"). Each answer leaks a few bits. Bulk extraction is costly; targeted confirmation is cheap. Mitigated by per-request audit, not prevented.
- **Uncalibrated probabilities.** LLM-stated probabilities are not calibrated like real Jev's. Clients should treat `confidence` as a rough signal.
- **Prompt injection of the internal agent** via `state` or log files. The agent can only emit schema-constrained values, so injected instructions cannot open a text channel; they can only skew answers.

## 11. Out of Scope (v1)

- Private free-text note for support staff (future work).
- Static API keys.
- Rate limiting beyond the concurrency limit.
- Real Jev or open-source System One engine integration.
- Question catalog / allow-listing.
