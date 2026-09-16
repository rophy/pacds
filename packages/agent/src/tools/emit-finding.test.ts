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
