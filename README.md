# PACDS

Platform-Managed Air-Gapped Code Diagnostic Service

PACDS enables SRE agents to ask bounded, code-aware questions about production incidents without ever accessing source code directly. Instead of distributing code to SRE agents (an exfiltration risk), SRE agents send production logs to PACDS, which reasons against the source code internally and returns structured diagnostic findings.

**Code never crosses the API boundary.**

## How It Works

SRE agents own the investigation. PACDS does not perform root cause analysis. It answers bounded questions like:

- "Look at these error logs from checkout-service. How likely are these caused by our code?"
- "The payment flow is returning 500s. Is there a known error handling path that could cause this?"

PACDS correlates the logs with the service's source code and returns structured findings with likelihood assessments and relevant areas, never raw code.

## API

### `POST /api/v1/diagnose`

**Headers:** `Authorization: Bearer <token>`

**Request:**

```json
{
  "service": "checkout-service",
  "log_query": {
    "trace_id": "abc123",
    "time_range": { "start": "2026-09-16T10:00:00Z", "end": "2026-09-16T10:05:00Z" },
    "labels": { "level": "error" }
  },
  "question": "Are these errors likely caused by our code?",
  "session_id": "sess-xxx"
}
```

- `service` maps to a Git repo via the service registry
- `log_query` references logs in Loki (trace ID, time range, label filters)
- `question` is a bounded, natural-language question
- `session_id` is optional; omit for a new session, include for follow-ups

**Response (finding):**

```json
{
  "findings": [
    {
      "likelihood": "high",
      "explanation": "The error logs show a NullPointerException in the payment processing path. The relevant module has a code path where the payment provider response is used without a null check after timeout.",
      "relevant_area": "payment processing module"
    }
  ],
  "session_id": "sess-xxx",
  "confidence": "high"
}
```

- `likelihood`: `high | medium | low | uncertain`
- `explanation`: natural language, max 500 characters
- `relevant_area`: general area, max 100 characters
- `confidence`: how much relevant code PACDS found (`high | medium | low`)

**Response (clarification):**

```json
{
  "type": "clarification",
  "question": "Which deployment version was running when the errors occurred?",
  "session_id": "sess-xxx"
}
```

When PACDS needs more context, it returns a clarification request. Send a follow-up with the same `session_id` to continue.

## Architecture

```
SRE Agent --> API Gateway --> Diagnostic Agent --> (Git + Loki + LLM)
                                                         |
                                              structured findings
                                                         |
SRE Agent <-- API Gateway <-- Diagnostic Agent <---------+
```

Three components with strict network isolation:

| Component | Role | Has Access To |
|---|---|---|
| **API Gateway** | Auth, rate limiting, output validation | Nothing sensitive |
| **Diagnostic Agent** | Reasons against code via LLM tool-calling | Git server, Loki, LLM |
| **LLM Inference** | Self-hosted model (vLLM) | Model weights only, no egress |

### Code Leakage Prevention

Three layers, defense in depth:

1. **Structured output by construction** -- The LLM responds only via typed tool calls (`emit_finding`, `ask_clarification`). There is no free-form text output path.

2. **Schema validation at the gateway** -- Every response must conform to the output schema with field length limits. Hard gate; nothing reaches the SRE agent without passing.

3. **Code-likeness heuristics** -- Applied to the `explanation` field. Checks syntax density, keyword clustering, and line structure. Responses that look like code are rejected.

### Network Isolation

Kubernetes network policies enforce strict boundaries:

- **Gateway** can only talk to agent pods
- **Agent** can only talk to the Git server, Loki, and LLM
- **LLM** has no egress at all

Even if application code has a bug, the network boundary holds.

### Service Registry

A pre-configured mapping from service names to Git repo paths, stored as a Kubernetes ConfigMap:

```
checkout-service  -->  teams/commerce/checkout
user-service      -->  teams/platform/user-mgmt
```

SRE agents reference services by name and never need to know repo paths.

## Deployment

PACDS is designed for on-premise Kubernetes with:

- Self-hosted LLM (vLLM with an open model) for true air-gap
- A Git server for source code access (GitLab, Gitea, GitHub Enterprise, etc.)
- Loki for production log queries

See `k8s/` for namespace, deployment, network policy, and service registry manifests.

## Packages

| Package | Description |
|---|---|
| `packages/shared` | Zod schemas, type definitions, code-likeness heuristics |
| `packages/gateway` | Fastify API gateway with auth, validation, output enforcement |
| `packages/agent` | Pi framework diagnostic agent with custom tools |
