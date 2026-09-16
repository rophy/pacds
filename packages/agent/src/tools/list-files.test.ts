import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createListFilesTool } from "./list-files.js";

describe("list-files tool", () => {
  let repoDir: string;

  beforeAll(async () => {
    repoDir = await mkdtemp(join(tmpdir(), "pacds-list-files-"));
    await mkdir(join(repoDir, "src"), { recursive: true });
    await writeFile(join(repoDir, "src", "index.ts"), "content");
  });

  afterAll(async () => {
    await rm(repoDir, { recursive: true, force: true });
  });

  it("successfully lists a directory inside the repo", async () => {
    const tool = createListFilesTool(repoDir);
    const result = await tool.execute("call-1", { path: "src" });
    expect(result.content[0].text).toContain("index.ts");
    expect(result.details.entryCount).toBe(1);
  });

  it("rejects a path traversal attempt", async () => {
    const tool = createListFilesTool(repoDir);
    const result = await tool.execute("call-2", { path: "../../etc" });
    expect(result.content[0].text).toContain("path traversal not allowed");
  });

  it("handles directory not found gracefully", async () => {
    const tool = createListFilesTool(repoDir);
    const result = await tool.execute("call-3", { path: "does-not-exist" });
    expect(result.content[0].text).toContain("Directory not found");
  });
});
