# PACDS Design Spec

Platform-Managed Air-Gapped Code Diagnostic Service

## Problem

Corporate source code is air-gapped to prevent IP leakage. SRE teams need to troubleshoot production incidents by correlating error logs with source code, but distributing code access to SRE agents is an unacceptable exfiltration risk. Prompt guardrails ("don't output code") are fundamentally unreliable against prompt injection and steganographic extraction.

## Solution: Inversion of Control

Instead of sending code to SRE agents, SRE agents send production logs to PACDS. PACDS reasons against the source code internally and returns structured diagnostic findings. Raw code never crosses the API boundary.

**PACDS is not a diagnostic engine.** It does not perform root cause analysis. SRE agents own the investigation. PACDS answers bounded, code-aware questions — e.g., "look at these error logs, how likely are these caused by our code?"

## Constraints

- On-premise Kubernetes deployment
- Self-hosted open LLM (vLLM/TGI) — true air-gap, nothing leaves the network
- Self-hosted GitLab for source code
- Loki (or equivalent) for production log access
- Built on Pi framework (TypeScript) — agent-core for reasoning, Chord for service contracts
- Multi-turn sessions — SRE agents ask follow-up questions as their investigation progresses

## Components

Three components with strict network isolation:

| Component | K8s Kind | Has Access To | Talks To |
|---|---|---|---|
| API Gateway | Deployment + Service (2+ replicas) | Nothing sensitive | SRE agents (inbound), Diagnostic Agent (internal) |
| Diagnostic Agent | Job (ephemeral per session) | GitLab, Loki, LLM | API Gateway only |
| LLM Inference | Deployment + Service (GPU nodes) | Model weights | Diagnostic Agent only |

### API Gateway

Stateless HTTP service. Handles:
- SRE agent authentication (service account tokens or mTLS)
- Rate limiting
- Session routing (create, resume, timeout)
- Output validation (the egress enforcement chokepoint)

Has no access to source code, logs, or the LLM. Communicates with the Diagnostic Agent via Chord's typed service contract.

### Diagnostic Agent

Pi agent-core running in an ephemeral K8s pod. One pod per active session. Orchestrates the LLM's tool-calling loop to reason about the SRE agent's question against source code and logs.

Custom tools:

| Tool | Purpose | Data Source |
|---|---|---|
| `lookup_repo` | Resolve service name to GitLab project | Service registry (ConfigMap/CRD) |
| `fetch_logs` | Fetch logs matching the SRE's query | Loki API |
| `search_code` | Search for symbols, strings, patterns | GitLab API or local clone |
| `read_file` | Read a file or range from the repo | GitLab API or local clone |
| `list_files` | Browse directory structure | GitLab API or local clone |
| `emit_finding` | Return a structured finding | Output (terminates reasoning) |
| `ask_clarification` | Ask SRE agent for more context | Output (pauses session) |

Code access: shallow-clone the repo at the relevant commit into an ephemeral volume. Destroyed when the session ends.

The LLM system prompt instructs the model to use tools and respond only via `emit_finding` or `ask_clarification`. It does not include "don't leak code" guardrails — structural constraints handle enforcement, not prompt instructions.

### LLM Inference

Self-hosted open model served via vLLM or TGI. Exposes an OpenAI-compatible API. Pi's `pi-ai` package connects via `createProvider()` with OpenAI compatibility settings.

No egress. Only accepts requests from Diagnostic Agent pods.

## Data Flow

```
SRE Agent → API Gateway → Diagnostic Agent → (GitLab + Loki + LLM) → Diagnostic Agent → API Gateway → SRE Agent
```

1. SRE agent sends a request with service name, log query reference, and a bounded question.
2. Gateway authenticates, creates or resumes a session, forwards to the Diagnostic Agent.
3. Diagnostic Agent uses Pi's tool-calling loop: looks up the repo, fetches logs from Loki, reasons against the code via the LLM.
4. LLM responds via structured tool call (`emit_finding`).
5. Gateway validates the response against the output schema, runs code-likeness checks, returns to SRE agent.

### Input Schema

```
{
  service: string         // maps to repo via service registry
  log_query: {            // reference to logs in Loki
    trace_id?: string
    time_range: { start: string, end: string }
    labels?: Record<string, string>
  }
  question: string        // bounded, natural language question
  session_id?: string     // for follow-up questions
}
```

### Output Schema

```
{
  findings: [{
    likelihood: "high" | "medium" | "low" | "uncertain"
    explanation: string   // natural language, length-limited
    relevant_area: string // e.g. "payment processing module"
  }]
  session_id: string
  confidence: "high" | "medium" | "low"  // how much relevant code was found
}
```

String field limits: `explanation` max 500 characters, `relevant_area` max 100 characters. Short enough to convey findings, too short to embed meaningful code blocks.

## Output Validation (Code Leakage Prevention)

Three layers, defense in depth:

### Layer 1: Structured Output by Construction

The LLM never produces free-form responses. Pi's tool-calling model constrains output to typed tool calls (`emit_finding`, `ask_clarification`). There is no "reply with text" path. This eliminates the most direct exfiltration vector.

### Layer 2: Schema Validation at the Gateway

Every response must conform to the output schema. String fields have maximum length limits. The gateway rejects anything that doesn't parse. Hard gate — nothing reaches the SRE agent without passing.

### Layer 3: Code-Likeness Heuristics

Applied to the `explanation` field (the most open-ended field):

- **Syntax density** — ratio of programming syntax characters (`{};()=></>`) to total characters. Code has high density; prose has low.
- **Line structure** — code has short, indented lines with consistent patterns.
- **Keyword clustering** — high density of language keywords (`function`, `import`, `class`, `return`, `const`) in a short span.

If any check triggers, the response is flagged or rejected.

### Threat Model Acknowledgment

No system can prevent a determined attacker from extracting some information through an LLM that has seen code. The goal is to make bulk exfiltration impractical and detectable. The audit log (every request, response, and session) is the backstop for detecting misuse patterns.

## Infrastructure

### Network Policies (Hard Security Boundary)

```
Gateway:
  ingress: SRE agent CIDR
  egress:  Diagnostic Agent pods only

Diagnostic Agent:
  ingress: Gateway pods only
  egress:  GitLab, Loki, LLM Service only

LLM Service:
  ingress: Diagnostic Agent pods only
  egress:  none
```

K8s network policies enforce this at the infrastructure level. Even if application code has a bug, the network boundary holds.

### Session Pod Lifecycle

1. Gateway requests a pod from the warm pool (or creates a new Job).
2. Pod gets a unique service account scoped to the authorized repos for that service.
3. Repo is shallow-cloned into an ephemeral volume.
4. Pod stays warm for follow-up questions.
5. After idle timeout (configurable, default 15 min), pod is terminated and volume destroyed.
6. Max session duration hard limit (default 1 hour).

### Warm Pod Pool

Pre-spin blank agent pods to avoid cold-start latency. On session start, assign from pool and clone the repo. Pool auto-scales based on demand.

### Auth

- SRE agents → Gateway: service account tokens or mTLS
- Gateway → Agent: internal pod-to-pod, secured by network policy
- Agent → GitLab: platform service account token, scoped per-repo via service registry

### Observability

- Gateway logs every request/response (audit trail)
- Agent sessions recorded via Pi's JsonlSessionRepo for post-incident review
- Metrics: session count, duration, LLM latency, validation rejections

## Chord Integration

The Gateway and Diagnostic Agent are two facets of one Chord application. Chord defines the typed contract between them and handles remote communication. However, Chord is a convenience layer — K8s network policies are the actual security enforcement. If Chord's transport fails, the result is a service error, not a security breach.

## Service Registry

A pre-configured mapping from service names to GitLab project paths. Maintained by the platform team.

Stored as a K8s ConfigMap or CRD. Examples:

```
checkout-service → gitlab.internal/teams/commerce/checkout
user-service → gitlab.internal/teams/platform/user-mgmt
```

The Diagnostic Agent's `lookup_repo` tool reads this registry. SRE agents reference services by name and never need to know repo paths.

## Testing Strategy

### Unit Tests

- Gateway: schema validation, code-likeness heuristics, auth middleware
- Agent tools: mock GitLab/Loki responses, verify tool behavior
- Service registry: mapping lookups, error handling

### Integration Tests

- Agent + mock GitLab + mock Loki + real LLM → verify structured output
- Gateway + agent → verify output validation catches code-heavy responses
- Multi-turn sessions → verify state persistence and resumption

### Security Tests

- **Adversarial prompts** — questions designed to trick code output ("explain the exact implementation line by line"). Verify structural constraints prevent leakage.
- **Schema fuzzing** — malformed responses, oversized fields, unexpected fields. Gateway must reject.
- **Network policy verification** — from an agent pod, attempt to reach SRE agent network. Must fail. Automated as a K8s test job.
- **Pod isolation** — verify one session cannot access another session's repo clone.

### Test Environment

- Test GitLab instance with a known sample repo
- Mock Loki with canned log data
- Real self-hosted LLM (security tests need the real model)
- Dedicated K8s namespace with production-equivalent network policies
