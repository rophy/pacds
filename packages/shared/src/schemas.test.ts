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
      log_provider: { type: "loki", url: "http://loki:3100" },
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
      log_provider: { type: "static" as const, lines: [] },
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
