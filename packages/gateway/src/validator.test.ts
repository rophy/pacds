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
