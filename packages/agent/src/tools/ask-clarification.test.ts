import { describe, it, expect } from "vitest";
import { createAskClarificationTool, type ClarificationRequest } from "./ask-clarification.js";

describe("ask_clarification tool", () => {
  it("has the correct tool name", () => {
    const clarifications: ClarificationRequest[] = [];
    const tool = createAskClarificationTool(clarifications);
    expect(tool.name).toBe("ask_clarification");
  });

  it("pushes question into clarifications array and terminates", async () => {
    const clarifications: ClarificationRequest[] = [];
    const tool = createAskClarificationTool(clarifications);
    const result = await tool.execute("call-1", { question: "Which version was deployed?" });
    expect(clarifications).toHaveLength(1);
    expect(clarifications[0].question).toBe("Which version was deployed?");
    expect(result.terminate).toBe(true);
  });

  it("accumulates multiple clarifications", async () => {
    const clarifications: ClarificationRequest[] = [];
    const tool = createAskClarificationTool(clarifications);
    await tool.execute("call-1", { question: "First question?" });
    await tool.execute("call-2", { question: "Second question?" });
    expect(clarifications).toHaveLength(2);
    expect(clarifications[1].question).toBe("Second question?");
  });
});
