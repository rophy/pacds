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
