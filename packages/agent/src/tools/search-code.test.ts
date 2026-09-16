import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtemp, writeFile, rm, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { createSearchCodeTool } from "./search-code.js";

describe("search_code tool", () => {
  let repoDir: string;

  beforeAll(async () => {
    repoDir = await mkdtemp(join(tmpdir(), "search-code-test-"));
    await mkdir(join(repoDir, "src"), { recursive: true });
    await writeFile(join(repoDir, "src", "app.ts"), "function handleRequest() {\n  return 'ok';\n}\n");
    await writeFile(join(repoDir, "src", "utils.ts"), "export function parseInput(data: string) {\n  return JSON.parse(data);\n}\n");
    await writeFile(join(repoDir, "README.md"), "# Test Project\nNo code here.\n");
  });

  afterAll(async () => {
    await rm(repoDir, { recursive: true, force: true });
  });

  it("has the correct tool name", () => {
    const tool = createSearchCodeTool(repoDir);
    expect(tool.name).toBe("search_code");
  });

  it("finds matches across files", async () => {
    const tool = createSearchCodeTool(repoDir);
    const result = await tool.execute("call-1", { pattern: "function" });
    const text = result.content[0].text;
    expect(text).toContain("handleRequest");
    expect(text).toContain("parseInput");
    expect(result.details.matchCount).toBeGreaterThanOrEqual(2);
  });

  it("filters by file glob", async () => {
    const tool = createSearchCodeTool(repoDir);
    const result = await tool.execute("call-2", { pattern: "function", file_glob: "*.ts" });
    const text = result.content[0].text;
    expect(text).toContain("function");
    expect(text).not.toContain("README");
  });

  it("returns no matches for unmatched pattern", async () => {
    const tool = createSearchCodeTool(repoDir);
    const result = await tool.execute("call-3", { pattern: "zzz_nonexistent_zzz" });
    expect(result.content[0].text).toBe("No matches found.");
    expect(result.details.matchCount).toBe(0);
  });
});
