import { describe, it, expect } from "vitest";
import { buildSystemPrompt } from "./system-prompt.js";

describe("buildSystemPrompt", () => {
  it("includes the service name and repo path", () => {
    const prompt = buildSystemPrompt("checkout-service", "teams/commerce/checkout");
    expect(prompt).toContain("checkout-service");
    expect(prompt).toContain("teams/commerce/checkout");
  });

  it("lists all available tools", () => {
    const prompt = buildSystemPrompt("my-service", "repo/path");
    for (const tool of ["lookup_repo", "fetch_logs", "search_code", "read_file", "list_files", "emit_finding", "ask_clarification"]) {
      expect(prompt).toContain(tool);
    }
  });

  it("instructs to respond only via emit_finding or ask_clarification", () => {
    const prompt = buildSystemPrompt("svc", "repo");
    expect(prompt).toContain("emit_finding");
    expect(prompt).toContain("ask_clarification");
    expect(prompt).toContain("never produce free-form text output");
  });
});
