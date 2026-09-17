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
      gitUrl: "",
      gitToken: "",
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
        log_provider: { type: "static", lines: [] },
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
