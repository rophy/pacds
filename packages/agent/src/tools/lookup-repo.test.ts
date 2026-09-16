import { describe, it, expect } from "vitest";
import { createLookupRepoTool } from "./lookup-repo.js";

describe("lookup-repo tool", () => {
  const registry: Record<string, string> = {
    "checkout-service": "teams/commerce/checkout",
    "user-service": "teams/platform/user-mgmt",
  };

  const tool = createLookupRepoTool(registry);

  it("has the correct name", () => {
    expect(tool.name).toBe("lookup_repo");
  });

  it("resolves a known service to its repo path", async () => {
    const result = await tool.execute("call-1", { service: "checkout-service" });
    expect(result.content[0]).toEqual({
      type: "text",
      text: expect.stringContaining("teams/commerce/checkout"),
    });
  });

  it("returns an error for unknown service", async () => {
    const result = await tool.execute("call-2", { service: "unknown-service" });
    expect(result.content[0]).toEqual({
      type: "text",
      text: expect.stringContaining("not found"),
    });
  });
});
