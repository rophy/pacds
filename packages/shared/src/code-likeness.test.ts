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
