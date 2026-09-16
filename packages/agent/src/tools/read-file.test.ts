import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createReadFileTool } from "./read-file.js";

describe("read-file tool", () => {
  let repoDir: string;

  beforeAll(async () => {
    repoDir = await mkdtemp(join(tmpdir(), "pacds-read-file-"));
    await mkdir(join(repoDir, "src"), { recursive: true });
    await writeFile(join(repoDir, "src", "index.ts"), "line1\nline2\nline3\n");
  });

  afterAll(async () => {
    await rm(repoDir, { recursive: true, force: true });
  });

  it("successfully reads a file inside the repo", async () => {
    const tool = createReadFileTool(repoDir);
    const result = await tool.execute("call-1", { path: "src/index.ts" });
    expect(result.content[0].text).toContain("line1");
    expect(result.details.lineCount).toBe(4);
  });

  it("rejects a path traversal attempt", async () => {
    const tool = createReadFileTool(repoDir);
    const result = await tool.execute("call-2", { path: "../../etc/passwd" });
    expect(result.content[0].text).toContain("path traversal not allowed");
  });

  it("handles file not found gracefully", async () => {
    const tool = createReadFileTool(repoDir);
    const result = await tool.execute("call-3", { path: "does/not/exist.ts" });
    expect(result.content[0].text).toContain("File not found");
  });
});
