import { describe, it, expect } from "vitest";
import { join } from "node:path";
import { isInsideRepo } from "./path-utils.js";

describe("isInsideRepo", () => {
  const repoDir = "/repos/checkout";

  it("returns true for a valid relative path", () => {
    expect(isInsideRepo(repoDir, join(repoDir, "src/index.ts"))).toBe(true);
  });

  it("returns true for the repo root itself", () => {
    expect(isInsideRepo(repoDir, join(repoDir, "."))).toBe(true);
  });

  it("returns false for a parent traversal", () => {
    expect(isInsideRepo(repoDir, join(repoDir, "../etc/passwd"))).toBe(false);
  });

  it("returns false for a sibling directory with a prefix collision", () => {
    expect(isInsideRepo(repoDir, "/repos/checkout-evil/secrets.ts")).toBe(false);
  });

  it("returns false for an absolute path outside the repo", () => {
    expect(isInsideRepo(repoDir, "/etc/passwd")).toBe(false);
  });
});
