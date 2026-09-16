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
