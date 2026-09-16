# PACDS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Platform-Managed Air-Gapped Code Diagnostic Service that answers bounded, code-aware questions from SRE agents by correlating production logs with source code, without exposing raw code.

**Architecture:** Two-tier service — an API Gateway (HTTP, stateless) and a Diagnostic Agent (Pi agent-core, ephemeral per session). The gateway handles auth, session routing, and output validation. The agent runs the LLM reasoning loop with tools for GitLab code access and Loki log fetching. Chord defines the typed contract between them. K8s network policies enforce the security boundary.

**Tech Stack:** TypeScript, Pi framework (@earendil-works/pi-coding-agent, @earendil-works/pi-ai, @earendil-works/chord), Fastify (HTTP server), Zod (schema validation), vitest (testing)

**Spec:** `docs/superpowers/specs/2026-09-16-pacds-design.md`

## Global Constraints

- Node.js >= 22
- TypeScript strict mode
- All packages from npm; no vendored dependencies
- No free-form text output from the LLM — all output via typed tool calls only
- String field limits: `explanation` max 500 chars, `relevant_area` max 100 chars
- Every HTTP response must pass schema validation before reaching the client

## Deferred (follow-up plan)

The following spec requirements are intentionally deferred to keep this plan focused on the core working system:

- **Chord typed contract** — gateway and agent currently communicate via plain HTTP/JSON. Replacing this with a Chord facet contract adds type safety and contract evolution but is not required for the system to function or be secure (K8s network policies are the hard boundary).
- **Observability** — audit logging (every request/response) and metrics (session count, duration, LLM latency, validation rejections) are important for production but not for the initial working implementation.
- **Warm pod pool** — the agent runs as a simple Deployment for now. Per-session ephemeral Jobs with a warm pool is a production optimization.
- **Repo cloning** — the agent tools assume a local clone exists at `repoBaseDir`. The clone lifecycle (shallow clone on session start, cleanup on session end) needs implementation.

---

### Task 1: Project Scaffolding & Shared Types

**Files:**
- Create: `package.json`
- Create: `tsconfig.json`
- Create: `packages/shared/package.json`
- Create: `packages/shared/tsconfig.json`
- Create: `packages/shared/src/schemas.ts`
- Create: `packages/shared/src/index.ts`
- Test: `packages/shared/src/schemas.test.ts`

**Interfaces:**
- Consumes: nothing
- Produces: `DiagnosticRequest`, `DiagnosticResponse`, `Finding`, `LogQuery` Zod schemas and inferred TypeScript types. `validateResponse(response: unknown): DiagnosticResponse` function. These are used by both the gateway (Task 2) and agent (Task 4).

- [ ] **Step 1: Initialize the monorepo**

```bash
npm init -y
```

Edit `package.json`:
```json
{
  "name": "pacds",
  "private": true,
  "workspaces": ["packages/*"],
  "scripts": {
    "build": "tsc --build",
    "test": "vitest run"
  }
}
```

Create `tsconfig.json`:
```json
{
  "compilerOptions": {
    "strict": true,
    "target": "ES2022",
    "module": "Node16",
    "moduleResolution": "Node16",
    "declaration": true,
    "composite": true,
    "outDir": "dist",
    "rootDir": "src",
    "esModuleInterop": true,
    "skipLibCheck": true
  }
}
```

- [ ] **Step 2: Create the shared package**

Create `packages/shared/package.json`:
```json
{
  "name": "@pacds/shared",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "main": "dist/index.js",
  "types": "dist/index.d.ts",
  "scripts": {
    "build": "tsc",
    "test": "vitest run"
  }
}
```

Create `packages/shared/tsconfig.json`:
```json
{
  "extends": "../../tsconfig.json",
  "compilerOptions": {
    "outDir": "dist",
    "rootDir": "src"
  },
  "include": ["src"]
}
```

- [ ] **Step 3: Install shared dependencies**

```bash
cd packages/shared
npm install zod
npm install -D vitest typescript
```

- [ ] **Step 4: Write the failing test for schemas**

Create `packages/shared/src/schemas.test.ts`:
```typescript
import { describe, it, expect } from "vitest";
import {
  DiagnosticRequestSchema,
  DiagnosticResponseSchema,
  validateResponse,
  type DiagnosticRequest,
  type DiagnosticResponse,
} from "./schemas.js";

describe("DiagnosticRequestSchema", () => {
  it("accepts a valid request with all fields", () => {
    const input: DiagnosticRequest = {
      service: "checkout-service",
      log_query: {
        trace_id: "abc123",
        time_range: { start: "2026-09-16T00:00:00Z", end: "2026-09-16T01:00:00Z" },
        labels: { env: "production" },
      },
      question: "How likely are these errors caused by our code?",
      session_id: "sess-001",
    };
    expect(DiagnosticRequestSchema.parse(input)).toEqual(input);
  });

  it("accepts a minimal request without optional fields", () => {
    const input = {
      service: "checkout-service",
      log_query: {
        time_range: { start: "2026-09-16T00:00:00Z", end: "2026-09-16T01:00:00Z" },
      },
      question: "Are these errors from our code?",
    };
    expect(DiagnosticRequestSchema.parse(input)).toEqual(input);
  });

  it("rejects a request missing required fields", () => {
    expect(() => DiagnosticRequestSchema.parse({ service: "x" })).toThrow();
  });
});

describe("DiagnosticResponseSchema", () => {
  it("accepts a valid response", () => {
    const response: DiagnosticResponse = {
      findings: [
        {
          likelihood: "high",
          explanation: "The error matches a known code path in the payment module.",
          relevant_area: "payment processing",
        },
      ],
      session_id: "sess-001",
      confidence: "high",
    };
    expect(DiagnosticResponseSchema.parse(response)).toEqual(response);
  });

  it("rejects explanation exceeding 500 characters", () => {
    const response = {
      findings: [
        {
          likelihood: "high",
          explanation: "x".repeat(501),
          relevant_area: "payments",
        },
      ],
      session_id: "sess-001",
      confidence: "high",
    };
    expect(() => DiagnosticResponseSchema.parse(response)).toThrow();
  });

  it("rejects relevant_area exceeding 100 characters", () => {
    const response = {
      findings: [
        {
          likelihood: "medium",
          explanation: "Some explanation.",
          relevant_area: "x".repeat(101),
        },
      ],
      session_id: "sess-001",
      confidence: "medium",
    };
    expect(() => DiagnosticResponseSchema.parse(response)).toThrow();
  });
});

describe("validateResponse", () => {
  it("returns a validated response for valid input", () => {
    const input = {
      findings: [
        {
          likelihood: "low",
          explanation: "Unlikely to be code-related.",
          relevant_area: "network layer",
        },
      ],
      session_id: "sess-002",
      confidence: "low",
    };
    expect(validateResponse(input)).toEqual(input);
  });

  it("throws for invalid input", () => {
    expect(() => validateResponse({ bad: "data" })).toThrow();
  });
});
```

- [ ] **Step 5: Run test to verify it fails**

```bash
cd packages/shared
npx vitest run src/schemas.test.ts
```

Expected: FAIL — module `./schemas.js` does not exist.

- [ ] **Step 6: Implement schemas**

Create `packages/shared/src/schemas.ts`:
```typescript
import { z } from "zod";

const LogQuerySchema = z.object({
  trace_id: z.string().optional(),
  time_range: z.object({
    start: z.string(),
    end: z.string(),
  }),
  labels: z.record(z.string()).optional(),
});

export const DiagnosticRequestSchema = z.object({
  service: z.string().min(1),
  log_query: LogQuerySchema,
  question: z.string().min(1),
  session_id: z.string().optional(),
});

const FindingSchema = z.object({
  likelihood: z.enum(["high", "medium", "low", "uncertain"]),
  explanation: z.string().max(500),
  relevant_area: z.string().max(100),
});

export const DiagnosticResponseSchema = z.object({
  findings: z.array(FindingSchema),
  session_id: z.string(),
  confidence: z.enum(["high", "medium", "low"]),
});

export type DiagnosticRequest = z.infer<typeof DiagnosticRequestSchema>;
export type DiagnosticResponse = z.infer<typeof DiagnosticResponseSchema>;
export type Finding = z.infer<typeof FindingSchema>;
export type LogQuery = z.infer<typeof LogQuerySchema>;

export function validateResponse(data: unknown): DiagnosticResponse {
  return DiagnosticResponseSchema.parse(data);
}
```

Create `packages/shared/src/index.ts`:
```typescript
export {
  DiagnosticRequestSchema,
  DiagnosticResponseSchema,
  validateResponse,
  type DiagnosticRequest,
  type DiagnosticResponse,
  type Finding,
  type LogQuery,
} from "./schemas.js";
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
cd packages/shared
npx vitest run src/schemas.test.ts
```

Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
git add packages/shared package.json tsconfig.json
git commit -m "feat: project scaffolding and shared diagnostic schemas"
```

---

### Task 2: Output Validation — Code-Likeness Heuristics

**Files:**
- Create: `packages/shared/src/code-likeness.ts`
- Modify: `packages/shared/src/index.ts`
- Test: `packages/shared/src/code-likeness.test.ts`

**Interfaces:**
- Consumes: nothing
- Produces: `checkCodeLikeness(text: string): CodeLikenessResult` where `CodeLikenessResult` is `{ passed: boolean; flags: string[] }`. Used by the gateway (Task 3) to validate the `explanation` field before returning responses.

- [ ] **Step 1: Write the failing test**

Create `packages/shared/src/code-likeness.test.ts`:
```typescript
import { describe, it, expect } from "vitest";
import { checkCodeLikeness } from "./code-likeness.js";

describe("checkCodeLikeness", () => {
  it("passes normal prose", () => {
    const result = checkCodeLikeness(
      "The error is likely caused by a timeout in the payment processing module when the upstream service is unavailable."
    );
    expect(result.passed).toBe(true);
    expect(result.flags).toEqual([]);
  });

  it("flags text with high syntax density", () => {
    const result = checkCodeLikeness(
      "if (user.isActive && user.role === 'admin') { return db.query(sql); }"
    );
    expect(result.passed).toBe(false);
    expect(result.flags).toContain("syntax_density");
  });

  it("flags text with keyword clustering", () => {
    const result = checkCodeLikeness(
      "function processPayment(amount) { const result = await fetch(url); return result.json(); }"
    );
    expect(result.passed).toBe(false);
    expect(result.flags).toContain("keyword_clustering");
  });

  it("flags text with code-like line structure", () => {
    const code = [
      "  const x = 1;",
      "  const y = 2;",
      "  if (x > y) {",
      "    return x;",
      "  }",
    ].join("\n");
    const result = checkCodeLikeness(code);
    expect(result.passed).toBe(false);
    expect(result.flags).toContain("line_structure");
  });

  it("passes technical prose that mentions code concepts", () => {
    const result = checkCodeLikeness(
      "The function responsible for processing payments likely throws an exception when the amount exceeds the configured maximum. This is a validation check, not a bug."
    );
    expect(result.passed).toBe(true);
  });

  it("passes empty string", () => {
    const result = checkCodeLikeness("");
    expect(result.passed).toBe(true);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd packages/shared
npx vitest run src/code-likeness.test.ts
```

Expected: FAIL — module `./code-likeness.js` does not exist.

- [ ] **Step 3: Implement code-likeness checks**

Create `packages/shared/src/code-likeness.ts`:
```typescript
export interface CodeLikenessResult {
  passed: boolean;
  flags: string[];
}

const SYNTAX_CHARS = new Set(["{", "}", ";", "(", ")", "=", ">", "<", "/", "[", "]"]);
const SYNTAX_DENSITY_THRESHOLD = 0.12;

const CODE_KEYWORDS = new Set([
  "function", "const", "let", "var", "return", "import", "export",
  "class", "if", "else", "for", "while", "async", "await", "throw",
  "new", "try", "catch", "switch", "case", "break", "continue",
  "def", "self", "elif", "except", "finally", "yield", "lambda",
  "public", "private", "protected", "static", "void", "int", "string",
]);
const KEYWORD_DENSITY_THRESHOLD = 0.08;

const INDENTED_LINE = /^[ \t]{2,}\S/;
const LINE_STRUCTURE_THRESHOLD = 0.6;
const MIN_LINES_FOR_STRUCTURE = 3;

function checkSyntaxDensity(text: string): boolean {
  if (text.length === 0) return false;
  let count = 0;
  for (const ch of text) {
    if (SYNTAX_CHARS.has(ch)) count++;
  }
  return count / text.length > SYNTAX_DENSITY_THRESHOLD;
}

function checkKeywordClustering(text: string): boolean {
  const words = text.toLowerCase().split(/\s+/);
  if (words.length === 0) return false;
  let count = 0;
  for (const word of words) {
    const cleaned = word.replace(/[^a-z]/g, "");
    if (CODE_KEYWORDS.has(cleaned)) count++;
  }
  return count / words.length > KEYWORD_DENSITY_THRESHOLD;
}

function checkLineStructure(text: string): boolean {
  const lines = text.split("\n").filter((l) => l.trim().length > 0);
  if (lines.length < MIN_LINES_FOR_STRUCTURE) return false;
  let indented = 0;
  for (const line of lines) {
    if (INDENTED_LINE.test(line)) indented++;
  }
  return indented / lines.length > LINE_STRUCTURE_THRESHOLD;
}

export function checkCodeLikeness(text: string): CodeLikenessResult {
  if (text.length === 0) return { passed: true, flags: [] };

  const flags: string[] = [];
  if (checkSyntaxDensity(text)) flags.push("syntax_density");
  if (checkKeywordClustering(text)) flags.push("keyword_clustering");
  if (checkLineStructure(text)) flags.push("line_structure");

  return { passed: flags.length === 0, flags };
}
```

- [ ] **Step 4: Export from index**

Add to `packages/shared/src/index.ts`:
```typescript
export { checkCodeLikeness, type CodeLikenessResult } from "./code-likeness.js";
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd packages/shared
npx vitest run src/code-likeness.test.ts
```

Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/shared/src/code-likeness.ts packages/shared/src/code-likeness.test.ts packages/shared/src/index.ts
git commit -m "feat: code-likeness heuristic for output validation"
```

---

### Task 3: API Gateway

**Files:**
- Create: `packages/gateway/package.json`
- Create: `packages/gateway/tsconfig.json`
- Create: `packages/gateway/src/server.ts`
- Create: `packages/gateway/src/sessions.ts`
- Create: `packages/gateway/src/validator.ts`
- Create: `packages/gateway/src/index.ts`
- Test: `packages/gateway/src/server.test.ts`
- Test: `packages/gateway/src/validator.test.ts`

**Interfaces:**
- Consumes: `DiagnosticRequestSchema`, `DiagnosticResponseSchema`, `validateResponse`, `checkCodeLikeness` from `@pacds/shared` (Task 1, Task 2)
- Produces: `createGatewayServer(config: GatewayConfig): FastifyInstance` — HTTP server with `POST /api/v1/diagnose` endpoint. `GatewayConfig` has `{ agentEndpoint: string; authTokens: string[] }`. The gateway calls the Diagnostic Agent at `agentEndpoint` and validates the response before returning to the client. Used as the main entry point (Task 6).

- [ ] **Step 1: Create the gateway package**

Create `packages/gateway/package.json`:
```json
{
  "name": "@pacds/gateway",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "main": "dist/index.js",
  "types": "dist/index.d.ts",
  "scripts": {
    "build": "tsc",
    "test": "vitest run",
    "start": "node dist/index.js"
  }
}
```

Create `packages/gateway/tsconfig.json`:
```json
{
  "extends": "../../tsconfig.json",
  "compilerOptions": {
    "outDir": "dist",
    "rootDir": "src"
  },
  "include": ["src"],
  "references": [{ "path": "../shared" }]
}
```

- [ ] **Step 2: Install gateway dependencies**

```bash
cd packages/gateway
npm install fastify @pacds/shared
npm install -D vitest typescript @types/node
```

- [ ] **Step 3: Write the failing test for the validator**

Create `packages/gateway/src/validator.test.ts`:
```typescript
import { describe, it, expect } from "vitest";
import { validateAgentOutput } from "./validator.js";

describe("validateAgentOutput", () => {
  it("accepts a valid response with clean explanation", () => {
    const result = validateAgentOutput({
      findings: [
        {
          likelihood: "high",
          explanation: "The error pattern matches a timeout in the payment module.",
          relevant_area: "payment processing",
        },
      ],
      session_id: "sess-001",
      confidence: "high",
    });
    expect(result.valid).toBe(true);
  });

  it("rejects a response with code-like explanation", () => {
    const result = validateAgentOutput({
      findings: [
        {
          likelihood: "high",
          explanation: "if (user.isActive && user.role === 'admin') { return db.query(sql); }",
          relevant_area: "auth module",
        },
      ],
      session_id: "sess-001",
      confidence: "high",
    });
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("code-likeness");
  });

  it("rejects a response that fails schema validation", () => {
    const result = validateAgentOutput({
      findings: "not an array",
      session_id: "sess-001",
    });
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("schema");
  });

  it("rejects a response with oversized explanation", () => {
    const result = validateAgentOutput({
      findings: [
        {
          likelihood: "low",
          explanation: "x".repeat(501),
          relevant_area: "module",
        },
      ],
      session_id: "sess-001",
      confidence: "low",
    });
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("schema");
  });
});
```

- [ ] **Step 4: Run test to verify it fails**

```bash
cd packages/gateway
npx vitest run src/validator.test.ts
```

Expected: FAIL — module `./validator.js` does not exist.

- [ ] **Step 5: Implement the validator**

Create `packages/gateway/src/validator.ts`:
```typescript
import { DiagnosticResponseSchema, checkCodeLikeness } from "@pacds/shared";
import type { DiagnosticResponse } from "@pacds/shared";

export interface ValidationResult {
  valid: boolean;
  response?: DiagnosticResponse;
  reason?: string;
}

export function validateAgentOutput(data: unknown): ValidationResult {
  const parsed = DiagnosticResponseSchema.safeParse(data);
  if (!parsed.success) {
    return { valid: false, reason: `schema: ${parsed.error.message}` };
  }

  for (const finding of parsed.data.findings) {
    const likeness = checkCodeLikeness(finding.explanation);
    if (!likeness.passed) {
      return {
        valid: false,
        reason: `code-likeness: explanation flagged for ${likeness.flags.join(", ")}`,
      };
    }
  }

  return { valid: true, response: parsed.data };
}
```

- [ ] **Step 6: Run validator tests**

```bash
cd packages/gateway
npx vitest run src/validator.test.ts
```

Expected: All tests PASS.

- [ ] **Step 7: Write the failing test for the HTTP server**

Create `packages/gateway/src/server.test.ts`:
```typescript
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { createGatewayServer } from "./server.js";
import type { FastifyInstance } from "fastify";

let server: FastifyInstance;

const MOCK_AGENT_RESPONSE = {
  findings: [
    {
      likelihood: "high" as const,
      explanation: "The error matches a known timeout pattern in the payment module.",
      relevant_area: "payment processing",
    },
  ],
  session_id: "sess-test",
  confidence: "high" as const,
};

describe("Gateway HTTP Server", () => {
  beforeAll(async () => {
    server = await createGatewayServer({
      agentEndpoint: "http://localhost:19999",
      authTokens: ["test-token-123"],
    });
  });

  afterAll(async () => {
    await server.close();
  });

  it("rejects unauthenticated requests", async () => {
    const response = await server.inject({
      method: "POST",
      url: "/api/v1/diagnose",
      payload: {
        service: "checkout-service",
        log_query: { time_range: { start: "2026-09-16T00:00:00Z", end: "2026-09-16T01:00:00Z" } },
        question: "Are these errors from our code?",
      },
    });
    expect(response.statusCode).toBe(401);
  });

  it("rejects requests with invalid schema", async () => {
    const response = await server.inject({
      method: "POST",
      url: "/api/v1/diagnose",
      headers: { authorization: "Bearer test-token-123" },
      payload: { bad: "data" },
    });
    expect(response.statusCode).toBe(400);
  });

  it("accepts a valid authenticated request", async () => {
    const response = await server.inject({
      method: "POST",
      url: "/api/v1/diagnose",
      headers: { authorization: "Bearer test-token-123" },
      payload: {
        service: "checkout-service",
        log_query: { time_range: { start: "2026-09-16T00:00:00Z", end: "2026-09-16T01:00:00Z" } },
        question: "Are these errors from our code?",
      },
    });
    // Will be 502 because mock agent endpoint is not running,
    // but it should NOT be 400 or 401 — it passed auth and validation
    expect(response.statusCode).not.toBe(400);
    expect(response.statusCode).not.toBe(401);
  });

  it("returns health check", async () => {
    const response = await server.inject({
      method: "GET",
      url: "/healthz",
    });
    expect(response.statusCode).toBe(200);
  });
});
```

- [ ] **Step 8: Run test to verify it fails**

```bash
cd packages/gateway
npx vitest run src/server.test.ts
```

Expected: FAIL — module `./server.js` does not exist.

- [ ] **Step 9: Implement the session store**

Create `packages/gateway/src/sessions.ts`:
```typescript
interface Session {
  id: string;
  service: string;
  createdAt: number;
  lastAccessedAt: number;
}

export class SessionStore {
  private sessions = new Map<string, Session>();
  private idleTimeoutMs: number;

  constructor(idleTimeoutMs = 15 * 60 * 1000) {
    this.idleTimeoutMs = idleTimeoutMs;
  }

  create(service: string): string {
    const id = `sess-${crypto.randomUUID()}`;
    this.sessions.set(id, {
      id,
      service,
      createdAt: Date.now(),
      lastAccessedAt: Date.now(),
    });
    return id;
  }

  touch(id: string): Session | undefined {
    const session = this.sessions.get(id);
    if (!session) return undefined;
    session.lastAccessedAt = Date.now();
    return session;
  }

  cleanup(): number {
    const now = Date.now();
    let removed = 0;
    for (const [id, session] of this.sessions) {
      if (now - session.lastAccessedAt > this.idleTimeoutMs) {
        this.sessions.delete(id);
        removed++;
      }
    }
    return removed;
  }
}
```

- [ ] **Step 10: Implement the HTTP server**

Create `packages/gateway/src/server.ts`:
```typescript
import Fastify from "fastify";
import type { FastifyInstance } from "fastify";
import { DiagnosticRequestSchema } from "@pacds/shared";
import { validateAgentOutput } from "./validator.js";
import { SessionStore } from "./sessions.js";

export interface GatewayConfig {
  agentEndpoint: string;
  authTokens: string[];
}

export async function createGatewayServer(
  config: GatewayConfig,
): Promise<FastifyInstance> {
  const server = Fastify({ logger: true });
  const sessions = new SessionStore();

  server.get("/healthz", async () => ({ status: "ok" }));

  server.post("/api/v1/diagnose", async (request, reply) => {
    const auth = request.headers.authorization;
    if (!auth || !auth.startsWith("Bearer ")) {
      return reply.status(401).send({ error: "Missing authorization" });
    }
    const token = auth.slice(7);
    if (!config.authTokens.includes(token)) {
      return reply.status(401).send({ error: "Invalid token" });
    }

    const parsed = DiagnosticRequestSchema.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: "Invalid request", details: parsed.error.message });
    }

    const diagnosticRequest = parsed.data;
    const sessionId = diagnosticRequest.session_id ?? sessions.create(diagnosticRequest.service);
    if (diagnosticRequest.session_id) {
      sessions.touch(diagnosticRequest.session_id);
    }

    let agentResponse: unknown;
    try {
      const res = await fetch(config.agentEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...diagnosticRequest, session_id: sessionId }),
      });
      agentResponse = await res.json();
    } catch (err) {
      request.log.error(err, "Failed to reach diagnostic agent");
      return reply.status(502).send({ error: "Diagnostic agent unavailable" });
    }

    const validation = validateAgentOutput(agentResponse);
    if (!validation.valid) {
      request.log.warn({ reason: validation.reason }, "Agent output rejected");
      return reply.status(422).send({ error: "Response rejected by output validation", reason: validation.reason });
    }

    request.log.info(
      { session_id: sessionId, service: diagnosticRequest.service },
      "Diagnostic response served",
    );
    return reply.send(validation.response);
  });

  return server;
}
```

Create `packages/gateway/src/index.ts`:
```typescript
import { createGatewayServer } from "./server.js";

export { createGatewayServer, type GatewayConfig } from "./server.js";
export { validateAgentOutput, type ValidationResult } from "./validator.js";
export { SessionStore } from "./sessions.js";

const port = parseInt(process.env.PORT ?? "3000", 10);
const server = await createGatewayServer({
  agentEndpoint: process.env.AGENT_ENDPOINT ?? "http://localhost:3001",
  authTokens: (process.env.AUTH_TOKENS ?? "").split(",").filter(Boolean),
});

await server.listen({ port, host: "0.0.0.0" });
```

- [ ] **Step 11: Run all gateway tests**

```bash
cd packages/gateway
npx vitest run
```

Expected: All tests PASS.

- [ ] **Step 12: Commit**

```bash
git add packages/gateway
git commit -m "feat: API gateway with auth, schema validation, and code-likeness checks"
```

---

### Task 4: Diagnostic Agent — Pi Integration with Custom Tools

**Files:**
- Create: `packages/agent/package.json`
- Create: `packages/agent/tsconfig.json`
- Create: `packages/agent/src/tools/lookup-repo.ts`
- Create: `packages/agent/src/tools/fetch-logs.ts`
- Create: `packages/agent/src/tools/search-code.ts`
- Create: `packages/agent/src/tools/read-file.ts`
- Create: `packages/agent/src/tools/list-files.ts`
- Create: `packages/agent/src/tools/emit-finding.ts`
- Create: `packages/agent/src/tools/ask-clarification.ts`
- Create: `packages/agent/src/agent.ts`
- Create: `packages/agent/src/system-prompt.ts`
- Create: `packages/agent/src/index.ts`
- Test: `packages/agent/src/tools/lookup-repo.test.ts`
- Test: `packages/agent/src/tools/emit-finding.test.ts`
- Test: `packages/agent/src/tools/fetch-logs.test.ts`

**Interfaces:**
- Consumes: `DiagnosticRequest` from `@pacds/shared` (Task 1). Pi SDK: `createAgentSession`, `SessionManager`, `ModelRuntime`, `defineTool` from `@earendil-works/pi-coding-agent`. `Type` from `typebox`. `StringEnum` from `@earendil-works/pi-ai`.
- Produces: `createDiagnosticAgent(config: AgentConfig): DiagnosticAgentRunner` where `AgentConfig` has `{ gitlabUrl: string; gitlabToken: string; lokiUrl: string; serviceRegistry: Record<string, string>; llmProvider: string; llmModel: string }` and `DiagnosticAgentRunner` has `run(request: DiagnosticRequest): Promise<AgentResult>`. `AgentResult` is `{ type: "finding"; response: DiagnosticResponse } | { type: "clarification"; question: string; session_id: string }`. Used by the agent HTTP server (Task 5).

- [ ] **Step 1: Create the agent package**

Create `packages/agent/package.json`:
```json
{
  "name": "@pacds/agent",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "main": "dist/index.js",
  "types": "dist/index.d.ts",
  "scripts": {
    "build": "tsc",
    "test": "vitest run",
    "start": "node dist/index.js"
  }
}
```

Create `packages/agent/tsconfig.json`:
```json
{
  "extends": "../../tsconfig.json",
  "compilerOptions": {
    "outDir": "dist",
    "rootDir": "src"
  },
  "include": ["src"],
  "references": [{ "path": "../shared" }]
}
```

- [ ] **Step 2: Install agent dependencies**

```bash
cd packages/agent
npm install @pacds/shared @earendil-works/pi-coding-agent @earendil-works/pi-ai @sinclair/typebox
npm install -D vitest typescript @types/node
```

- [ ] **Step 3: Write the failing test for lookup-repo tool**

Create `packages/agent/src/tools/lookup-repo.test.ts`:
```typescript
import { describe, it, expect } from "vitest";
import { createLookupRepoTool } from "./lookup-repo.js";

describe("lookup-repo tool", () => {
  const registry: Record<string, string> = {
    "checkout-service": "teams/commerce/checkout",
    "user-service": "teams/platform/user-mgmt",
  };

  const tool = createLookupRepoTool(registry);

  it("has the correct name", () => {
    expect(tool.name).toBe("lookup_repo");
  });

  it("resolves a known service to its repo path", async () => {
    const result = await tool.execute("call-1", { service: "checkout-service" });
    expect(result.content[0]).toEqual({
      type: "text",
      text: expect.stringContaining("teams/commerce/checkout"),
    });
  });

  it("returns an error for unknown service", async () => {
    const result = await tool.execute("call-2", { service: "unknown-service" });
    expect(result.content[0]).toEqual({
      type: "text",
      text: expect.stringContaining("not found"),
    });
  });
});
```

- [ ] **Step 4: Run test to verify it fails**

```bash
cd packages/agent
npx vitest run src/tools/lookup-repo.test.ts
```

Expected: FAIL — module does not exist.

- [ ] **Step 5: Implement lookup-repo tool**

Create `packages/agent/src/tools/lookup-repo.ts`:
```typescript
import { Type } from "@sinclair/typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";

export function createLookupRepoTool(registry: Record<string, string>) {
  return defineTool({
    name: "lookup_repo",
    label: "Lookup Repo",
    description: "Resolve a service name to its GitLab project path using the service registry.",
    parameters: Type.Object({
      service: Type.String({ description: "The service name to look up" }),
    }),
    async execute(_toolCallId, params) {
      const repoPath = registry[params.service];
      if (!repoPath) {
        return {
          content: [{ type: "text" as const, text: `Service "${params.service}" not found in registry.` }],
          details: {},
        };
      }
      return {
        content: [{ type: "text" as const, text: `Repository: ${repoPath}` }],
        details: { repoPath },
      };
    },
  });
}
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd packages/agent
npx vitest run src/tools/lookup-repo.test.ts
```

Expected: PASS.

- [ ] **Step 7: Write the failing test for emit-finding tool**

Create `packages/agent/src/tools/emit-finding.test.ts`:
```typescript
import { describe, it, expect } from "vitest";
import { createEmitFindingTool } from "./emit-finding.js";
import type { Finding } from "@pacds/shared";

describe("emit-finding tool", () => {
  it("has the correct name", () => {
    const findings: Finding[] = [];
    const tool = createEmitFindingTool(findings);
    expect(tool.name).toBe("emit_finding");
  });

  it("captures the finding and returns confirmation", async () => {
    const findings: Finding[] = [];
    const tool = createEmitFindingTool(findings);

    const result = await tool.execute("call-1", {
      likelihood: "high",
      explanation: "The error matches a timeout pattern.",
      relevant_area: "payment processing",
    });

    expect(findings).toHaveLength(1);
    expect(findings[0]).toEqual({
      likelihood: "high",
      explanation: "The error matches a timeout pattern.",
      relevant_area: "payment processing",
    });
    expect(result.content[0]).toEqual({
      type: "text",
      text: expect.stringContaining("recorded"),
    });
    expect(result.terminate).toBe(true);
  });
});
```

- [ ] **Step 8: Implement emit-finding tool**

Create `packages/agent/src/tools/emit-finding.ts`:
```typescript
import { Type } from "@sinclair/typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import type { Finding } from "@pacds/shared";

export function createEmitFindingTool(findings: Finding[]) {
  return defineTool({
    name: "emit_finding",
    label: "Emit Finding",
    description: "Record a diagnostic finding. Use this to report your conclusion about the code's relationship to the observed errors. This is the ONLY way to return results.",
    parameters: Type.Object({
      likelihood: StringEnum(["high", "medium", "low", "uncertain"] as const),
      explanation: Type.String({ description: "Natural language explanation, max 500 chars", maxLength: 500 }),
      relevant_area: Type.String({ description: "Which part of the codebase is relevant, max 100 chars", maxLength: 100 }),
    }),
    async execute(_toolCallId, params) {
      findings.push({
        likelihood: params.likelihood,
        explanation: params.explanation,
        relevant_area: params.relevant_area,
      });
      return {
        content: [{ type: "text" as const, text: "Finding recorded. You may emit more findings or stop." }],
        details: {},
        terminate: true,
      };
    },
  });
}
```

- [ ] **Step 9: Run test to verify it passes**

```bash
cd packages/agent
npx vitest run src/tools/emit-finding.test.ts
```

Expected: PASS.

- [ ] **Step 10: Write the failing test for fetch-logs tool**

Create `packages/agent/src/tools/fetch-logs.test.ts`:
```typescript
import { describe, it, expect, vi } from "vitest";
import { createFetchLogsTool } from "./fetch-logs.js";

describe("fetch-logs tool", () => {
  it("fetches logs from Loki and returns them", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        data: {
          result: [
            {
              values: [
                ["1694880000000000000", "ERROR: payment timeout after 30s"],
                ["1694880001000000000", "WARN: retrying payment for order 12345"],
              ],
            },
          ],
        },
      }),
    });

    const tool = createFetchLogsTool("http://loki:3100", mockFetch);

    const result = await tool.execute("call-1", {
      query: '{service="checkout-service"}',
      start: "2026-09-16T00:00:00Z",
      end: "2026-09-16T01:00:00Z",
      limit: 100,
    });

    expect(result.content[0]).toEqual({
      type: "text",
      text: expect.stringContaining("payment timeout"),
    });
    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it("handles Loki errors gracefully", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      text: async () => "Internal Server Error",
    });

    const tool = createFetchLogsTool("http://loki:3100", mockFetch);

    const result = await tool.execute("call-1", {
      query: '{service="checkout-service"}',
      start: "2026-09-16T00:00:00Z",
      end: "2026-09-16T01:00:00Z",
    });

    expect(result.content[0]).toEqual({
      type: "text",
      text: expect.stringContaining("Failed"),
    });
  });
});
```

- [ ] **Step 11: Implement fetch-logs tool**

Create `packages/agent/src/tools/fetch-logs.ts`:
```typescript
import { Type } from "@sinclair/typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";

type FetchFn = typeof globalThis.fetch;

export function createFetchLogsTool(lokiUrl: string, fetchFn: FetchFn = globalThis.fetch) {
  return defineTool({
    name: "fetch_logs",
    label: "Fetch Logs",
    description: "Fetch production logs from Loki for a given LogQL query and time range.",
    parameters: Type.Object({
      query: Type.String({ description: "LogQL query, e.g. {service=\"checkout-service\"}" }),
      start: Type.String({ description: "Start time in RFC3339 format" }),
      end: Type.String({ description: "End time in RFC3339 format" }),
      limit: Type.Optional(Type.Integer({ description: "Max log lines to return", minimum: 1, maximum: 1000 })),
    }),
    async execute(_toolCallId, params) {
      const url = new URL(`${lokiUrl}/loki/api/v1/query_range`);
      url.searchParams.set("query", params.query);
      url.searchParams.set("start", params.start);
      url.searchParams.set("end", params.end);
      url.searchParams.set("limit", String(params.limit ?? 100));

      try {
        const res = await fetchFn(url.toString());
        if (!res.ok) {
          const body = await res.text();
          return {
            content: [{ type: "text" as const, text: `Failed to fetch logs: HTTP ${res.status} — ${body}` }],
            details: {},
          };
        }

        const data = await res.json() as {
          data: { result: Array<{ values: Array<[string, string]> }> };
        };

        const lines = data.data.result.flatMap((stream) =>
          stream.values.map(([_ts, line]) => line)
        );

        if (lines.length === 0) {
          return {
            content: [{ type: "text" as const, text: "No log lines matched the query." }],
            details: {},
          };
        }

        return {
          content: [{ type: "text" as const, text: lines.join("\n") }],
          details: { lineCount: lines.length },
        };
      } catch (err) {
        return {
          content: [{ type: "text" as const, text: `Failed to fetch logs: ${err}` }],
          details: {},
        };
      }
    },
  });
}
```

- [ ] **Step 12: Run test to verify it passes**

```bash
cd packages/agent
npx vitest run src/tools/fetch-logs.test.ts
```

Expected: PASS.

- [ ] **Step 13: Implement remaining tools (search-code, read-file, list-files, ask-clarification)**

Create `packages/agent/src/tools/search-code.ts`:
```typescript
import { Type } from "@sinclair/typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export function createSearchCodeTool(repoDir: string) {
  return defineTool({
    name: "search_code",
    label: "Search Code",
    description: "Search for a pattern in the cloned source code repository using grep.",
    parameters: Type.Object({
      pattern: Type.String({ description: "Search pattern (regex)" }),
      file_glob: Type.Optional(Type.String({ description: "File glob to restrict search, e.g. '*.ts'" })),
    }),
    async execute(_toolCallId, params) {
      try {
        const args = ["-rn", "--include", params.file_glob ?? "*", params.pattern, "."];
        const { stdout } = await execFileAsync("grep", args, {
          cwd: repoDir,
          maxBuffer: 1024 * 1024,
        });
        const lines = stdout.split("\n").filter(Boolean).slice(0, 50);
        return {
          content: [{ type: "text" as const, text: lines.join("\n") || "No matches found." }],
          details: { matchCount: lines.length },
        };
      } catch {
        return {
          content: [{ type: "text" as const, text: "No matches found." }],
          details: { matchCount: 0 },
        };
      }
    },
  });
}
```

Create `packages/agent/src/tools/read-file.ts`:
```typescript
import { Type } from "@sinclair/typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { readFile } from "node:fs/promises";
import { join } from "node:path";

export function createReadFileTool(repoDir: string) {
  return defineTool({
    name: "read_file",
    label: "Read File",
    description: "Read a file from the cloned source code repository.",
    parameters: Type.Object({
      path: Type.String({ description: "File path relative to the repo root" }),
      start_line: Type.Optional(Type.Integer({ description: "Start line (1-indexed)", minimum: 1 })),
      end_line: Type.Optional(Type.Integer({ description: "End line (1-indexed)", minimum: 1 })),
    }),
    async execute(_toolCallId, params) {
      const fullPath = join(repoDir, params.path);
      if (!fullPath.startsWith(repoDir)) {
        return {
          content: [{ type: "text" as const, text: "Error: path traversal not allowed." }],
          details: {},
        };
      }
      try {
        const content = await readFile(fullPath, "utf-8");
        let lines = content.split("\n");
        if (params.start_line || params.end_line) {
          const start = (params.start_line ?? 1) - 1;
          const end = params.end_line ?? lines.length;
          lines = lines.slice(start, end);
        }
        const numbered = lines.map((line, i) => {
          const lineNum = (params.start_line ?? 1) + i;
          return `${lineNum}\t${line}`;
        });
        return {
          content: [{ type: "text" as const, text: numbered.join("\n") }],
          details: { lineCount: lines.length },
        };
      } catch {
        return {
          content: [{ type: "text" as const, text: `File not found: ${params.path}` }],
          details: {},
        };
      }
    },
  });
}
```

Create `packages/agent/src/tools/list-files.ts`:
```typescript
import { Type } from "@sinclair/typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { readdir } from "node:fs/promises";
import { join } from "node:path";

export function createListFilesTool(repoDir: string) {
  return defineTool({
    name: "list_files",
    label: "List Files",
    description: "List files and directories in the cloned source code repository.",
    parameters: Type.Object({
      path: Type.Optional(Type.String({ description: "Directory path relative to repo root. Defaults to root." })),
    }),
    async execute(_toolCallId, params) {
      const dir = join(repoDir, params.path ?? ".");
      if (!dir.startsWith(repoDir)) {
        return {
          content: [{ type: "text" as const, text: "Error: path traversal not allowed." }],
          details: {},
        };
      }
      try {
        const entries = await readdir(dir, { withFileTypes: true });
        const listing = entries.map((e) => `${e.isDirectory() ? "d" : "f"} ${e.name}`);
        return {
          content: [{ type: "text" as const, text: listing.join("\n") || "Empty directory." }],
          details: { entryCount: entries.length },
        };
      } catch {
        return {
          content: [{ type: "text" as const, text: `Directory not found: ${params.path ?? "."}` }],
          details: {},
        };
      }
    },
  });
}
```

Create `packages/agent/src/tools/ask-clarification.ts`:
```typescript
import { Type } from "@sinclair/typebox";
import { defineTool } from "@earendil-works/pi-coding-agent";

export interface ClarificationRequest {
  question: string;
}

export function createAskClarificationTool(clarifications: ClarificationRequest[]) {
  return defineTool({
    name: "ask_clarification",
    label: "Ask Clarification",
    description: "Ask the SRE agent for more context about the incident. Use when you need additional information to answer their question.",
    parameters: Type.Object({
      question: Type.String({ description: "The clarifying question to ask the SRE agent" }),
    }),
    async execute(_toolCallId, params) {
      clarifications.push({ question: params.question });
      return {
        content: [{ type: "text" as const, text: "Clarification request sent. Waiting for response." }],
        details: {},
        terminate: true,
      };
    },
  });
}
```

- [ ] **Step 14: Implement the system prompt**

Create `packages/agent/src/system-prompt.ts`:
```typescript
export function buildSystemPrompt(service: string, repoPath: string): string {
  return `You are a code diagnostic assistant. Your job is to examine source code and answer questions about whether observed production errors are caused by the code.

You are investigating service "${service}" (repo: ${repoPath}).

Available tools:
- lookup_repo: Resolve service names to repo paths
- fetch_logs: Fetch production logs from Loki
- search_code: Search for patterns in the source code
- read_file: Read source code files
- list_files: Browse the repository structure
- emit_finding: Report your diagnostic finding (REQUIRED — this is the only way to return results)
- ask_clarification: Ask the SRE agent for more context

Workflow:
1. Use fetch_logs to retrieve the relevant production logs
2. Analyze the error patterns, stack traces, and error messages in the logs
3. Use search_code and read_file to examine the relevant source code
4. Use emit_finding to report whether the errors are likely caused by the code

Rules:
- Always respond via emit_finding or ask_clarification — never produce free-form text output
- Focus on answering the specific question asked — do not perform unbounded investigation
- Report what the code reveals, not what you assume about the production environment`;
}
```

- [ ] **Step 15: Implement the agent runner**

Create `packages/agent/src/agent.ts`:
```typescript
import {
  createAgentSession,
  ModelRuntime,
  SessionManager,
} from "@earendil-works/pi-coding-agent";
import type { DiagnosticRequest, DiagnosticResponse, Finding } from "@pacds/shared";
import { createLookupRepoTool } from "./tools/lookup-repo.js";
import { createFetchLogsTool } from "./tools/fetch-logs.js";
import { createSearchCodeTool } from "./tools/search-code.js";
import { createReadFileTool } from "./tools/read-file.js";
import { createListFilesTool } from "./tools/list-files.js";
import { createEmitFindingTool } from "./tools/emit-finding.js";
import {
  createAskClarificationTool,
  type ClarificationRequest,
} from "./tools/ask-clarification.js";
import { buildSystemPrompt } from "./system-prompt.js";

export interface AgentConfig {
  gitlabUrl: string;
  gitlabToken: string;
  lokiUrl: string;
  serviceRegistry: Record<string, string>;
  llmProvider: string;
  llmModel: string;
  repoBaseDir: string;
}

export type AgentResult =
  | { type: "finding"; response: DiagnosticResponse }
  | { type: "clarification"; question: string; session_id: string };

export interface DiagnosticAgentRunner {
  run(request: DiagnosticRequest): Promise<AgentResult>;
}

export function createDiagnosticAgent(config: AgentConfig): DiagnosticAgentRunner {
  return {
    async run(request: DiagnosticRequest): Promise<AgentResult> {
      const repoPath = config.serviceRegistry[request.service];
      if (!repoPath) {
        return {
          type: "finding",
          response: {
            findings: [{
              likelihood: "uncertain",
              explanation: `Service "${request.service}" not found in registry.`,
              relevant_area: "unknown",
            }],
            session_id: request.session_id ?? "unknown",
            confidence: "low",
          },
        };
      }

      const repoDir = `${config.repoBaseDir}/${request.service}`;

      const findings: Finding[] = [];
      const clarifications: ClarificationRequest[] = [];

      const tools = [
        createLookupRepoTool(config.serviceRegistry),
        createFetchLogsTool(config.lokiUrl),
        createSearchCodeTool(repoDir),
        createReadFileTool(repoDir),
        createListFilesTool(repoDir),
        createEmitFindingTool(findings),
        createAskClarificationTool(clarifications),
      ];

      const modelRuntime = await ModelRuntime.create();
      const { session } = await createAgentSession({
        sessionManager: SessionManager.inMemory(),
        modelRuntime,
        customTools: tools,
        tools: tools.map((t) => t.name),
        systemPromptOverride: buildSystemPrompt(request.service, repoPath),
      });

      const prompt = [
        `Service: ${request.service}`,
        `Log query: ${JSON.stringify(request.log_query)}`,
        `Question: ${request.question}`,
      ].join("\n");

      await session.prompt(prompt);
      await session.dispose();

      if (clarifications.length > 0) {
        return {
          type: "clarification",
          question: clarifications[0].question,
          session_id: request.session_id ?? "unknown",
        };
      }

      const sessionId = request.session_id ?? `sess-${crypto.randomUUID()}`;
      const confidence = findings.length > 0 ? "high" : "low";

      return {
        type: "finding",
        response: {
          findings,
          session_id: sessionId,
          confidence: confidence as "high" | "medium" | "low",
        },
      };
    },
  };
}
```

Create `packages/agent/src/index.ts`:
```typescript
export {
  createDiagnosticAgent,
  type AgentConfig,
  type AgentResult,
  type DiagnosticAgentRunner,
} from "./agent.js";
```

- [ ] **Step 16: Run all agent tests**

```bash
cd packages/agent
npx vitest run
```

Expected: All tests PASS.

- [ ] **Step 17: Commit**

```bash
git add packages/agent
git commit -m "feat: diagnostic agent with Pi integration and custom tools"
```

---

### Task 5: Agent HTTP Server

**Files:**
- Create: `packages/agent/src/server.ts`
- Modify: `packages/agent/src/index.ts`
- Test: `packages/agent/src/server.test.ts`

**Interfaces:**
- Consumes: `createDiagnosticAgent(config: AgentConfig): DiagnosticAgentRunner` from Task 4. `DiagnosticRequestSchema` from `@pacds/shared` (Task 1).
- Produces: `createAgentServer(config: AgentConfig): FastifyInstance` — HTTP server with `POST /` endpoint that the gateway calls. Listens on port 3001 by default.

- [ ] **Step 1: Write the failing test**

Create `packages/agent/src/server.test.ts`:
```typescript
import { describe, it, expect, beforeAll, afterAll, vi } from "vitest";
import { createAgentServer } from "./server.js";
import type { FastifyInstance } from "fastify";

vi.mock("./agent.js", () => ({
  createDiagnosticAgent: () => ({
    run: vi.fn().mockResolvedValue({
      type: "finding",
      response: {
        findings: [
          {
            likelihood: "high",
            explanation: "The timeout pattern matches the payment retry logic.",
            relevant_area: "payment processing",
          },
        ],
        session_id: "sess-test",
        confidence: "high",
      },
    }),
  }),
}));

let server: FastifyInstance;

describe("Agent HTTP Server", () => {
  beforeAll(async () => {
    server = await createAgentServer({
      gitlabUrl: "http://gitlab.internal",
      gitlabToken: "test-token",
      lokiUrl: "http://loki:3100",
      serviceRegistry: { "checkout-service": "teams/commerce/checkout" },
      llmProvider: "vllm-local",
      llmModel: "meta-llama-3.1-70b",
      repoBaseDir: "/tmp/repos",
    });
  });

  afterAll(async () => {
    await server.close();
  });

  it("accepts a valid diagnostic request", async () => {
    const response = await server.inject({
      method: "POST",
      url: "/",
      payload: {
        service: "checkout-service",
        log_query: {
          time_range: { start: "2026-09-16T00:00:00Z", end: "2026-09-16T01:00:00Z" },
        },
        question: "Are these errors from our code?",
        session_id: "sess-test",
      },
    });
    expect(response.statusCode).toBe(200);
    const body = response.json();
    expect(body.type).toBe("finding");
  });

  it("rejects invalid requests", async () => {
    const response = await server.inject({
      method: "POST",
      url: "/",
      payload: { bad: "data" },
    });
    expect(response.statusCode).toBe(400);
  });

  it("returns health check", async () => {
    const response = await server.inject({
      method: "GET",
      url: "/healthz",
    });
    expect(response.statusCode).toBe(200);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd packages/agent
npx vitest run src/server.test.ts
```

Expected: FAIL — module `./server.js` does not exist.

- [ ] **Step 3: Implement the agent HTTP server**

Create `packages/agent/src/server.ts`:
```typescript
import Fastify from "fastify";
import type { FastifyInstance } from "fastify";
import { DiagnosticRequestSchema } from "@pacds/shared";
import { createDiagnosticAgent, type AgentConfig } from "./agent.js";

export async function createAgentServer(config: AgentConfig): Promise<FastifyInstance> {
  const server = Fastify({ logger: true });
  const agent = createDiagnosticAgent(config);

  server.get("/healthz", async () => ({ status: "ok" }));

  server.post("/", async (request, reply) => {
    const parsed = DiagnosticRequestSchema.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: "Invalid request", details: parsed.error.message });
    }

    try {
      const result = await agent.run(parsed.data);
      return reply.send(result);
    } catch (err) {
      request.log.error(err, "Agent execution failed");
      return reply.status(500).send({ error: "Agent execution failed" });
    }
  });

  return server;
}
```

- [ ] **Step 4: Update agent index.ts to include server and add main entry**

Update `packages/agent/src/index.ts`:
```typescript
export {
  createDiagnosticAgent,
  type AgentConfig,
  type AgentResult,
  type DiagnosticAgentRunner,
} from "./agent.js";
export { createAgentServer } from "./server.js";

import { createAgentServer } from "./server.js";

const port = parseInt(process.env.PORT ?? "3001", 10);
const server = await createAgentServer({
  gitlabUrl: process.env.GITLAB_URL ?? "http://gitlab.internal",
  gitlabToken: process.env.GITLAB_TOKEN ?? "",
  lokiUrl: process.env.LOKI_URL ?? "http://loki:3100",
  serviceRegistry: JSON.parse(process.env.SERVICE_REGISTRY ?? "{}"),
  llmProvider: process.env.LLM_PROVIDER ?? "vllm-local",
  llmModel: process.env.LLM_MODEL ?? "meta-llama-3.1-70b",
  repoBaseDir: process.env.REPO_BASE_DIR ?? "/tmp/repos",
});

await server.listen({ port, host: "0.0.0.0" });
```

- [ ] **Step 5: Run all agent tests**

```bash
cd packages/agent
npx vitest run
```

Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/agent/src/server.ts packages/agent/src/server.test.ts packages/agent/src/index.ts
git commit -m "feat: agent HTTP server for gateway-to-agent communication"
```

---

### Task 6: vLLM Provider Extension

**Files:**
- Create: `packages/agent/src/vllm-provider.ts`
- Test: `packages/agent/src/vllm-provider.test.ts`

**Interfaces:**
- Consumes: `ExtensionAPI` from `@earendil-works/pi-coding-agent`
- Produces: Pi extension that registers a `vllm-local` provider pointing at the self-hosted vLLM endpoint. Loaded by the agent at startup via `additionalExtensionPaths` in `DefaultResourceLoader`.

- [ ] **Step 1: Write the failing test**

Create `packages/agent/src/vllm-provider.test.ts`:
```typescript
import { describe, it, expect, vi } from "vitest";
import { registerVllmProvider } from "./vllm-provider.js";

describe("vllm-provider", () => {
  it("registers a provider with the correct config", () => {
    const mockPi = {
      registerProvider: vi.fn(),
    };

    registerVllmProvider(mockPi as any, {
      baseUrl: "http://vllm:8000/v1",
      modelId: "meta-llama-3.1-70b",
      modelName: "Llama 3.1 70B",
      contextWindow: 128000,
    });

    expect(mockPi.registerProvider).toHaveBeenCalledWith("vllm-local", {
      name: "vLLM Local",
      baseUrl: "http://vllm:8000/v1",
      apiKey: "$VLLM_API_KEY",
      api: "openai-completions",
      models: [
        expect.objectContaining({
          id: "meta-llama-3.1-70b",
          name: "Llama 3.1 70B",
          contextWindow: 128000,
        }),
      ],
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd packages/agent
npx vitest run src/vllm-provider.test.ts
```

Expected: FAIL.

- [ ] **Step 3: Implement the vLLM provider**

Create `packages/agent/src/vllm-provider.ts`:
```typescript
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export interface VllmConfig {
  baseUrl: string;
  modelId: string;
  modelName: string;
  contextWindow: number;
}

export function registerVllmProvider(pi: ExtensionAPI, config: VllmConfig): void {
  pi.registerProvider("vllm-local", {
    name: "vLLM Local",
    baseUrl: config.baseUrl,
    apiKey: "$VLLM_API_KEY",
    api: "openai-completions",
    models: [
      {
        id: config.modelId,
        name: config.modelName,
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: config.contextWindow,
        maxTokens: 4096,
      },
    ],
  });
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd packages/agent
npx vitest run src/vllm-provider.test.ts
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/agent/src/vllm-provider.ts packages/agent/src/vllm-provider.test.ts
git commit -m "feat: vLLM provider extension for self-hosted LLM"
```

---

### Task 7: Dockerfiles & K8s Manifests

**Files:**
- Create: `packages/gateway/Dockerfile`
- Create: `packages/agent/Dockerfile`
- Create: `k8s/namespace.yaml`
- Create: `k8s/gateway-deployment.yaml`
- Create: `k8s/agent-job-template.yaml`
- Create: `k8s/network-policies.yaml`
- Create: `k8s/service-registry-configmap.yaml`

**Interfaces:**
- Consumes: Gateway server (Task 3) on port 3000, Agent server (Task 5) on port 3001
- Produces: Deployable K8s manifests and container images

- [ ] **Step 1: Create gateway Dockerfile**

Create `packages/gateway/Dockerfile`:
```dockerfile
FROM node:22-slim AS builder
WORKDIR /app
COPY package.json tsconfig.json ./
COPY packages/shared ./packages/shared
COPY packages/gateway ./packages/gateway
RUN npm install --workspace=packages/shared --workspace=packages/gateway
RUN npm run build --workspace=packages/shared --workspace=packages/gateway

FROM node:22-slim
WORKDIR /app
COPY --from=builder /app/packages/gateway/dist ./dist
COPY --from=builder /app/packages/gateway/package.json ./
COPY --from=builder /app/node_modules ./node_modules
ENV NODE_ENV=production
EXPOSE 3000
USER node
CMD ["node", "dist/index.js"]
```

- [ ] **Step 2: Create agent Dockerfile**

Create `packages/agent/Dockerfile`:
```dockerfile
FROM node:22-slim AS builder
WORKDIR /app
COPY package.json tsconfig.json ./
COPY packages/shared ./packages/shared
COPY packages/agent ./packages/agent
RUN npm install --workspace=packages/shared --workspace=packages/agent
RUN npm run build --workspace=packages/shared --workspace=packages/agent

FROM node:22-slim
RUN apt-get update && apt-get install -y git grep && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /app/packages/agent/dist ./dist
COPY --from=builder /app/packages/agent/package.json ./
COPY --from=builder /app/node_modules ./node_modules
ENV NODE_ENV=production
EXPOSE 3001
USER node
CMD ["node", "dist/index.js"]
```

- [ ] **Step 3: Create K8s namespace**

Create `k8s/namespace.yaml`:
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: pacds
  labels:
    app.kubernetes.io/part-of: pacds
```

- [ ] **Step 4: Create network policies**

Create `k8s/network-policies.yaml`:
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: gateway-policy
  namespace: pacds
spec:
  podSelector:
    matchLabels:
      app: pacds-gateway
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - namespaceSelector: {}
      ports:
        - port: 3000
  egress:
    - to:
        - podSelector:
            matchLabels:
              app: pacds-agent
      ports:
        - port: 3001
    - to:
        - namespaceSelector: {}
      ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-policy
  namespace: pacds
spec:
  podSelector:
    matchLabels:
      app: pacds-agent
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: pacds-gateway
      ports:
        - port: 3001
  egress:
    - to:
        - podSelector:
            matchLabels:
              app: pacds-llm
      ports:
        - port: 8000
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
      ports:
        - port: 443
        - port: 80
        - port: 3100
    - to:
        - namespaceSelector: {}
      ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: llm-policy
  namespace: pacds
spec:
  podSelector:
    matchLabels:
      app: pacds-llm
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: pacds-agent
      ports:
        - port: 8000
  egress: []
```

- [ ] **Step 5: Create service registry ConfigMap**

Create `k8s/service-registry-configmap.yaml`:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: service-registry
  namespace: pacds
data:
  registry.json: |
    {
      "checkout-service": "teams/commerce/checkout",
      "user-service": "teams/platform/user-mgmt"
    }
```

- [ ] **Step 6: Create gateway deployment**

Create `k8s/gateway-deployment.yaml`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pacds-gateway
  namespace: pacds
spec:
  replicas: 2
  selector:
    matchLabels:
      app: pacds-gateway
  template:
    metadata:
      labels:
        app: pacds-gateway
    spec:
      containers:
        - name: gateway
          image: pacds-gateway:latest
          ports:
            - containerPort: 3000
          env:
            - name: PORT
              value: "3000"
            - name: AGENT_ENDPOINT
              value: "http://pacds-agent:3001"
            - name: AUTH_TOKENS
              valueFrom:
                secretKeyRef:
                  name: pacds-auth
                  key: tokens
          readinessProbe:
            httpGet:
              path: /healthz
              port: 3000
            initialDelaySeconds: 5
          livenessProbe:
            httpGet:
              path: /healthz
              port: 3000
            initialDelaySeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 256Mi
---
apiVersion: v1
kind: Service
metadata:
  name: pacds-gateway
  namespace: pacds
spec:
  selector:
    app: pacds-gateway
  ports:
    - port: 3000
      targetPort: 3000
```

- [ ] **Step 7: Create agent job template**

Create `k8s/agent-job-template.yaml`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pacds-agent
  namespace: pacds
  annotations:
    description: "Placeholder deployment. In production, replace with per-session Jobs."
spec:
  replicas: 1
  selector:
    matchLabels:
      app: pacds-agent
  template:
    metadata:
      labels:
        app: pacds-agent
    spec:
      containers:
        - name: agent
          image: pacds-agent:latest
          ports:
            - containerPort: 3001
          env:
            - name: PORT
              value: "3001"
            - name: GITLAB_URL
              value: "http://gitlab.internal"
            - name: GITLAB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: pacds-gitlab
                  key: token
            - name: LOKI_URL
              value: "http://loki:3100"
            - name: LLM_PROVIDER
              value: "vllm-local"
            - name: LLM_MODEL
              value: "meta-llama-3.1-70b"
            - name: REPO_BASE_DIR
              value: "/repos"
            - name: SERVICE_REGISTRY
              valueFrom:
                configMapKeyRef:
                  name: service-registry
                  key: registry.json
          volumeMounts:
            - name: repo-storage
              mountPath: /repos
          readinessProbe:
            httpGet:
              path: /healthz
              port: 3001
            initialDelaySeconds: 10
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: 2000m
              memory: 2Gi
      volumes:
        - name: repo-storage
          emptyDir:
            sizeLimit: 5Gi
---
apiVersion: v1
kind: Service
metadata:
  name: pacds-agent
  namespace: pacds
spec:
  selector:
    app: pacds-agent
  ports:
    - port: 3001
      targetPort: 3001
```

- [ ] **Step 8: Commit**

```bash
git add packages/gateway/Dockerfile packages/agent/Dockerfile k8s/
git commit -m "feat: Dockerfiles and K8s manifests with network policies"
```

---

### Task 8: End-to-End Integration Test

**Files:**
- Create: `tests/e2e/diagnostic-flow.test.ts`
- Create: `tests/e2e/setup.ts`

**Interfaces:**
- Consumes: `createGatewayServer` from `@pacds/gateway` (Task 3), `createAgentServer` from `@pacds/agent` (Task 5). All shared types from `@pacds/shared` (Task 1).
- Produces: End-to-end test that verifies the full diagnostic flow: gateway → agent → mock GitLab/Loki → LLM mock → validated response.

- [ ] **Step 1: Write the e2e test**

Create `tests/e2e/setup.ts`:
```typescript
import { createGatewayServer } from "@pacds/gateway";
import type { FastifyInstance } from "fastify";

export async function setupGateway(agentPort: number): Promise<FastifyInstance> {
  return createGatewayServer({
    agentEndpoint: `http://localhost:${agentPort}`,
    authTokens: ["e2e-test-token"],
  });
}
```

Create `tests/e2e/diagnostic-flow.test.ts`:
```typescript
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { setupGateway } from "./setup.js";
import Fastify from "fastify";
import type { FastifyInstance } from "fastify";

let gateway: FastifyInstance;
let mockAgent: FastifyInstance;
const MOCK_AGENT_PORT = 19876;

describe("End-to-end diagnostic flow", () => {
  beforeAll(async () => {
    mockAgent = Fastify();
    mockAgent.post("/", async () => ({
      type: "finding",
      response: {
        findings: [
          {
            likelihood: "high",
            explanation: "The timeout pattern in the logs correlates with the retry configuration in the payment module.",
            relevant_area: "payment processing",
          },
        ],
        session_id: "sess-e2e",
        confidence: "high",
      },
    }));
    await mockAgent.listen({ port: MOCK_AGENT_PORT });

    gateway = await setupGateway(MOCK_AGENT_PORT);
  });

  afterAll(async () => {
    await gateway.close();
    await mockAgent.close();
  });

  it("processes a diagnostic request end-to-end", async () => {
    const response = await gateway.inject({
      method: "POST",
      url: "/api/v1/diagnose",
      headers: { authorization: "Bearer e2e-test-token" },
      payload: {
        service: "checkout-service",
        log_query: {
          trace_id: "abc123",
          time_range: { start: "2026-09-16T00:00:00Z", end: "2026-09-16T01:00:00Z" },
        },
        question: "These checkout errors started after the last deploy. Are they caused by our code?",
      },
    });

    expect(response.statusCode).toBe(200);
    const body = response.json();
    expect(body.findings).toHaveLength(1);
    expect(body.findings[0].likelihood).toBe("high");
    expect(body.session_id).toBe("sess-e2e");
  });

  it("rejects a response containing code-like content", async () => {
    await mockAgent.close();

    const codeLeakAgent = Fastify();
    codeLeakAgent.post("/", async () => ({
      type: "finding",
      response: {
        findings: [
          {
            likelihood: "high",
            explanation: "function processPayment(amount) { const result = await fetch(url); return result.json(); }",
            relevant_area: "payment processing",
          },
        ],
        session_id: "sess-leak",
        confidence: "high",
      },
    }));
    await codeLeakAgent.listen({ port: MOCK_AGENT_PORT });

    const response = await gateway.inject({
      method: "POST",
      url: "/api/v1/diagnose",
      headers: { authorization: "Bearer e2e-test-token" },
      payload: {
        service: "checkout-service",
        log_query: {
          time_range: { start: "2026-09-16T00:00:00Z", end: "2026-09-16T01:00:00Z" },
        },
        question: "Show me the code",
      },
    });

    expect(response.statusCode).toBe(422);

    await codeLeakAgent.close();

    mockAgent = Fastify();
    mockAgent.post("/", async () => ({
      type: "finding",
      response: {
        findings: [
          {
            likelihood: "high",
            explanation: "The timeout pattern correlates with retry logic.",
            relevant_area: "payment processing",
          },
        ],
        session_id: "sess-e2e",
        confidence: "high",
      },
    }));
    await mockAgent.listen({ port: MOCK_AGENT_PORT });
  });

  it("rejects unauthenticated requests", async () => {
    const response = await gateway.inject({
      method: "POST",
      url: "/api/v1/diagnose",
      payload: {
        service: "checkout-service",
        log_query: {
          time_range: { start: "2026-09-16T00:00:00Z", end: "2026-09-16T01:00:00Z" },
        },
        question: "Are these errors from our code?",
      },
    });

    expect(response.statusCode).toBe(401);
  });
});
```

- [ ] **Step 2: Run the e2e test**

```bash
npx vitest run tests/e2e/diagnostic-flow.test.ts
```

Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit -m "test: end-to-end diagnostic flow integration test"
```
