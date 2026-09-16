import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { createGatewayServer } from "./server.js";
import Fastify from "fastify";
import type { FastifyInstance } from "fastify";

let server: FastifyInstance;
let mockAgent: FastifyInstance;
const MOCK_AGENT_PORT = 19899;

function buildMockAgentResult(sessionId: string) {
  return {
    type: "finding" as const,
    response: {
      findings: [
        {
          likelihood: "high" as const,
          explanation: "The error matches a known timeout pattern in the payment module.",
          relevant_area: "payment processing",
        },
      ],
      session_id: sessionId,
      confidence: "high" as const,
    },
  };
}

describe("Gateway HTTP Server", () => {
  beforeAll(async () => {
    mockAgent = Fastify();
    mockAgent.post("/", async (request) => {
      const body = request.body as { session_id: string };
      return buildMockAgentResult(body.session_id);
    });
    await mockAgent.listen({ port: MOCK_AGENT_PORT });

    server = await createGatewayServer({
      agentEndpoint: `http://localhost:${MOCK_AGENT_PORT}`,
      authTokens: ["test-token-123"],
    });
  });

  afterAll(async () => {
    await server.close();
    await mockAgent.close();
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
    expect(response.statusCode).toBe(200);
    const body = response.json();
    expect(body.findings).toHaveLength(1);
    expect(body.session_id).toMatch(/^sess-/);
  });

  it("preserves the gateway-issued session_id across a follow-up request", async () => {
    const first = await server.inject({
      method: "POST",
      url: "/api/v1/diagnose",
      headers: { authorization: "Bearer test-token-123" },
      payload: {
        service: "checkout-service",
        log_query: { time_range: { start: "2026-09-16T00:00:00Z", end: "2026-09-16T01:00:00Z" } },
        question: "Are these errors from our code?",
      },
    });
    const firstSessionId = first.json().session_id;

    const second = await server.inject({
      method: "POST",
      url: "/api/v1/diagnose",
      headers: { authorization: "Bearer test-token-123" },
      payload: {
        service: "checkout-service",
        session_id: firstSessionId,
        log_query: { time_range: { start: "2026-09-16T00:00:00Z", end: "2026-09-16T01:00:00Z" } },
        question: "Any update?",
      },
    });

    expect(second.json().session_id).toBe(firstSessionId);
  });

  it("returns health check", async () => {
    const response = await server.inject({
      method: "GET",
      url: "/healthz",
    });
    expect(response.statusCode).toBe(200);
  });
});
